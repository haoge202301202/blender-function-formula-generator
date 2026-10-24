# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""公式解析与安全求值模块（纯 Python，不 import bpy，方便单独测试）。

设计说明（给以后维护的 AI / 人看）
==================================
1. 用户输入的是"数学表达式"，例如 sin(x) * 2、cos(y)、x*x + y*y。
2. 求值前先用 AST 白名单做静态检查：只允许数学运算、白名单函数、常量与变量 x / y。
   任何属性访问（除 math.xxx）、下标、lambda、推导式、字符串、import、赋值等都会被拒绝。
   这样即使有人写 __import__('os').system(...) 也会在语法检查阶段被拦下。
3. 通过检查后再编译成 code object，并只在受限命名空间中 eval：

       env = {"__builtins__": None, "math": math, "x": x, "y": y}
       env.update(FUNCTIONS)      # sin / cos / ...（用户不必写 math. 前缀）
       env.update(CONSTANTS)      # pi / e / tau

   __builtins__ 设为 None，公式里拿不到 open / import / getattr 等任何内置函数。
   公式里能调用的"函数"只有 FUNCTIONS 白名单里的纯数学函数。

维护提示（扩展点）
==================
* 新增数学函数：往 FUNCTIONS 里加一行；如果几何节点版本也要支持，
  同步更新 gn_nodes.py 的 _GN_FUNCTIONS 映射。
* 新增常量：往 CONSTANTS 里加一行；几何节点端同步 _GN_CONSTANTS。
* 想允许新的语法（如 and/or、条件表达式等）：先把节点类型加进 _ALLOWED_NODE_TYPES，
  再在 _Validator 里写对应的 visit_xxx 精确限定允许的形状，不要直接放行整棵树。
* 采样函数 sample_1d / sample_2d 与几何生成完全解耦，
  以后加"极坐标 / 参数方程 / 复平面"等新模式可以复用它们。
"""
from __future__ import annotations

import ast
import difflib
import math
import re
from typing import Callable, List, Tuple

__all__ = [
    "FormulaError",
    "Formula",
    "FUNCTIONS",
    "CONSTANTS",
    "validate_text",
    "sample_1d",
    "sample_2d",
    "sample_param_curve",
    "sample_param_surface",
    "preprocess_expression",
    "PARAMETERS",
    "PARAM_CURVE_VARIABLES",
    "PARAM_SURFACE_VARIABLES",
]

# ---------------------------------------------------------------------------
# 一、公式里允许使用的数学函数与常量（白名单即文档）
# ---------------------------------------------------------------------------

# 单次求值的表达式长度 / AST 节点数上限：防止用户粘贴超长表达式拖死 UI
MAX_EXPRESSION_LENGTH = 500
MAX_AST_NODES = 300
# 幂运算指数上限：防止 10 ** 1000000 这种整数爆炸（float 幂本身会抛 OverflowError）
MAX_CONSTANT_POW_EXPONENT = 512
# 字面量数值上限：防止超大整数参与运算
MAX_LITERAL_ABS = 1e15


def _sign(value: float) -> float:
    """符号函数：返回 -1.0 / 0.0 / 1.0（math 库没有 sign）。"""
    return float((value > 0) - (value < 0))


def _clamp(value: float, low: float, high: float) -> float:
    """把 value 限制在 [low, high] 区间；公式里可写 clamp(x, -1, 1)。"""
    return low if value < low else high if value > high else value


FUNCTIONS: dict[str, Callable[..., float]] = {
    # 三角函数
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    # 指数 / 对数 / 开方
    "exp": math.exp,
    "log": math.log,           # 自然对数
    "ln": math.log,            # 自然对数别名，方便书写
    "log2": math.log2,
    "log10": math.log10,
    "sqrt": math.sqrt,
    "pow": math.pow,
    "hypot": math.hypot,
    # 取整 / 绝对值 / 符号
    "abs": abs,
    "fabs": math.fabs,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": round,
    "trunc": math.trunc,
    "sign": _sign,
    # 其它常用数学函数
    "min": min,
    "max": max,
    "fmod": math.fmod,
    "degrees": math.degrees,
    "radians": math.radians,
    "clamp": _clamp,
}

CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,           # 2*pi
}

# 变量名：公式里可以写 x 和 y（两者在"圆管"模式下都等于采样参数 t，见 sample_1d 注释）
_VARIABLE_NAMES = frozenset({"x", "y"})

# 受限命名空间的基础部分。注意 __builtins__ 为 None —— 这是安全的关键。
_BASE_ENV: dict = {
    "__builtins__": None,
    "math": math,
    **FUNCTIONS,
    **CONSTANTS,
}


# ---------------------------------------------------------------------------
# 二、AST 白名单校验
# ---------------------------------------------------------------------------

class FormulaError(ValueError):
    """公式非法或求值失败。str(exc) 会直接显示在 Blender 面板/状态栏里。"""


# 允许的语法节点类型（White-list；_Validator 对每类做进一步形状限制）
_ALLOWED_BINARY_OPS: tuple[type, ...] = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)
_ALLOWED_UNARY_OPS: tuple[type, ...] = (ast.UAdd, ast.USub, ast.Not)
_ALLOWED_COMPARE_OPS: tuple[type, ...] = (
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
)

# 默认变量集（高度场模式）；参数模式会由 Formula 传入自己的变量集
_DEFAULT_VARIABLES = ("x", "y")
_ALLOWED_NAMES = frozenset(_DEFAULT_VARIABLES) | frozenset(FUNCTIONS) | frozenset(CONSTANTS)

# 公式里可用的"系数参数"（v1.3）：静态版本用面板值求值，几何节点版本是节点输入，
# 可以连接噪声/场景时间/编号等字段来让系数随空间或时间变化。
PARAMETERS = ("a", "b", "c", "d")


# 常量折叠上限：校验阶段把"纯常量子表达式"先算一遍，
# 提前拒绝 2 ** (2 ** 20)、10 ** 400 这类会让求值阶段爆内存/溢出的写法。
_MAX_CONST_RESULT = MAX_LITERAL_ABS


def _bounded_constant(value: float) -> float:
    """常量计算结果必须有限且不能太大，否则直接报错。"""
    if isinstance(value, complex):
        raise FormulaError("常量计算得到复数结果")
    try:
        number = float(value)
    except OverflowError:
        raise FormulaError("常量表达式结果超出浮点范围") from None
    if math.isnan(number):
        raise FormulaError("常量表达式得到 NaN")
    if math.isinf(number):
        raise FormulaError("常量表达式结果超出浮点范围")
    if abs(number) > _MAX_CONST_RESULT:
        raise FormulaError(
            f"常量表达式结果过大（{number:g}，绝对值上限 {_MAX_CONST_RESULT:g}）"
        )
    return number


def _const_eval(node: ast.AST, depth: int = 0):
    """尝试把"不含变量"的子树算成常量；含 x/y 时返回 None。

    与正常求值的区别：所有中间结果都做大小/有限性检查，
    保证校验阶段就能拦住指数爆炸（例如 2 ** (2 ** 20)）。
    """
    if depth > 64:
        raise FormulaError("公式嵌套层级过深")
    if isinstance(node, ast.Expression):
        return _const_eval(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node.value, (int, float)):
            return _bounded_constant(float(node.value))
        return None
    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            return _bounded_constant(float(CONSTANTS[node.id]))
        return None
    if isinstance(node, ast.Attribute):
        if node.attr in CONSTANTS:
            return _bounded_constant(float(CONSTANTS[node.attr]))
        return None
    if isinstance(node, ast.UnaryOp):
        value = _const_eval(node.operand, depth + 1)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return _bounded_constant(-value)
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.Not):
            return float(not value)
        return None
    if isinstance(node, ast.BinOp):
        left = _const_eval(node.left, depth + 1)
        right = _const_eval(node.right, depth + 1)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_CONSTANT_POW_EXPONENT:
                    raise FormulaError(
                        f"指数 {right:g} 超出安全范围（绝对值上限 {MAX_CONSTANT_POW_EXPONENT}）"
                    )
                result = left ** right
                if isinstance(result, int) and abs(result) > _MAX_CONST_RESULT:
                    raise FormulaError("常量表达式结果过大（指数爆炸）")
                return _bounded_constant(result)
            if isinstance(node.op, (ast.Div, ast.FloorDiv)) and right == 0:
                raise FormulaError("常量表达式出现除以 0")
            if isinstance(node.op, ast.Mod) and right == 0:
                raise FormulaError("常量表达式出现对 0 取模")
            if isinstance(node.op, ast.Add):
                return _bounded_constant(left + right)
            if isinstance(node.op, ast.Sub):
                return _bounded_constant(left - right)
            if isinstance(node.op, ast.Mult):
                return _bounded_constant(left * right)
            if isinstance(node.op, ast.Div):
                return _bounded_constant(left / right)
            if isinstance(node.op, ast.FloorDiv):
                return _bounded_constant(float(left // right))
            if isinstance(node.op, ast.Mod):
                return _bounded_constant(float(left % right))
        except OverflowError:
            raise FormulaError("常量表达式结果超出浮点范围") from None
        return None
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in FUNCTIONS:
            return None
        args = [_const_eval(arg, depth + 1) for arg in node.args]
        if any(arg is None for arg in args):
            return None
        try:
            result = FUNCTIONS[name](*args)
        except Exception as exc:  # 定义域错误 / 除零等
            raise FormulaError(f"常量计算 {name}() 失败：{type(exc).__name__}: {exc}") from None
        return _bounded_constant(result)
    if isinstance(node, (ast.Compare, ast.BoolOp, ast.IfExp)):
        # 常量条件表达式：能折叠就折叠，含变量则跳过静态检查
        try:
            return _bounded_constant(float(eval(_compile_constant_subtree(node), dict(_BASE_ENV))))
        except FormulaError:
            raise
        except Exception:
            return None
    return None


def _compile_constant_subtree(node: ast.AST):
    """只用于常量折叠的编译（子树已在白名单校验中放行）。"""
    return compile(ast.Expression(body=node), "<函数公式常量>", "eval")


class _Validator(ast.NodeVisitor):
    """AST 白名单校验器：任何不在白名单里的语法都会抛 FormulaError。

    allowed_variables 由 Formula 传入：高度场模式是 ("x", "y")，
    参数曲线模式是 ("t",)，参数曲面模式是 ("u", "v")。
    """

    def __init__(self, allowed_variables=("x", "y")):
        self.allowed_names = frozenset(allowed_variables) | frozenset(FUNCTIONS) | frozenset(CONSTANTS)

    def generic_visit(self, node: ast.AST) -> None:
        raise FormulaError(f"公式中不允许使用 {type(node).__name__} 语法")

    def visit_Expression(self, node: ast.Expression) -> None:
        self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, bool):
            # True/False 当作 0/1 使用没问题，放行
            return
        if not isinstance(value, (int, float)):
            raise FormulaError(f"不支持的常量类型：{type(value).__name__}")
        if abs(value) > MAX_LITERAL_ABS:
            raise FormulaError(
                f"常量 {value} 太大（绝对值上限 {MAX_LITERAL_ABS:g}）"
            )

    # Python 3.8+ 统一为 Constant，但保留别名以防旧版本语法树
    visit_Num = visit_Constant  # type: ignore[attr-defined]

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.allowed_names:
            return
        suggestions = difflib.get_close_matches(node.id, sorted(self.allowed_names), n=2)
        hint = f"，你是不是想写：{', '.join(suggestions)}？" if suggestions else ""
        raise FormulaError(f"未知变量或函数名：{node.id!r}{hint}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # 只允许 math.函数 / math.常量，别的属性访问一律拒绝
        if not (isinstance(node.value, ast.Name) and node.value.id == "math"):
            raise FormulaError("只允许访问 math 模块下的数学函数，例如 math.sin(x)")
        if node.attr.startswith("__"):
            raise FormulaError("不允许访问双下划线属性")
        if node.attr not in FUNCTIONS and node.attr not in CONSTANTS:
            raise FormulaError(f"math 模块里不允许使用：{node.attr}")

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not isinstance(node.op, _ALLOWED_BINARY_OPS):
            raise FormulaError(f"不支持的运算符：{type(node.op).__name__}")
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant):
            # 常量指数做上限检查：避免 2**999999 这类整数爆炸
            exponent = node.right.value
            if isinstance(exponent, (int, float)) and abs(exponent) > MAX_CONSTANT_POW_EXPONENT:
                raise FormulaError(
                    f"指数 {exponent} 超出安全范围（绝对值上限 {MAX_CONSTANT_POW_EXPONENT}）"
                )
        self.visit(node.left)
        self.visit(node.right)
        _const_eval(node)          # 纯常量表达式：提前做安全范围检查

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if not isinstance(node.op, _ALLOWED_UNARY_OPS):
            raise FormulaError(f"不支持的一元运算符：{type(node.op).__name__}")
        self.visit(node.operand)
        _const_eval(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise FormulaError("不支持的条件运算符")
        for value in node.values:
            self.visit(value)
        _const_eval(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        # 条件表达式 a if cond else b：结果会被转成 0/1 参与数值运算
        self.visit(node.test)
        self.visit(node.body)
        self.visit(node.orelse)
        _const_eval(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op in node.ops:
            if not isinstance(op, _ALLOWED_COMPARE_OPS):
                raise FormulaError(f"不支持的比较运算符：{type(op).__name__}")
        self.visit(node.left)
        for comparator in node.comparators:
            self.visit(comparator)
        _const_eval(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            self.visit_Attribute(func)       # 会校验必须是 math.xxx 白名单
            name = func.attr
        elif isinstance(func, ast.Name):
            if func.id not in FUNCTIONS:
                raise FormulaError(f"不允许调用函数：{func.id}（只支持数学函数白名单）")
            name = func.id
        else:
            raise FormulaError("不支持的函数调用形式")
        if node.keywords:
            raise FormulaError(f"{name}() 不支持关键字参数")
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise FormulaError("不支持 *args 展开")
            self.visit(arg)
        _const_eval(node)


def _check_formula_basics(source: str) -> str:
    """长度、空白等基础检查，返回清理后的源码。"""
    text = source.strip()
    if not text:
        raise FormulaError("公式为空，请输入数学表达式，例如：sin(x) * 2")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise FormulaError(f"公式太长（最多 {MAX_EXPRESSION_LENGTH} 个字符）")
    return text


def _to_float(value, x: float = 0.0, y: float = 0.0, coordinates: str = "") -> float:
    """把 eval 的返回值转成有限浮点数；bool / int 都接受。"""
    where = coordinates or f"x={x:.6g}, y={y:.6g}"
    if isinstance(value, bool):
        value = float(value)
    if isinstance(value, complex):
        raise FormulaError(f"公式在 {where} 处得到复数结果（请检查负数的分数次幂）")
    try:
        number = float(value)
    except OverflowError:
        raise FormulaError(f"公式在 {where} 处的结果超出浮点范围（数值太大）") from None
    except (TypeError, ValueError):
        raise FormulaError(f"公式在 {where} 处的返回值不是数字：{value!r}") from None
    if math.isnan(number):
        raise FormulaError(f"公式在 {where} 处得到 NaN（请检查定义域）")
    if math.isinf(number):
        raise FormulaError(f"公式在 {where} 处得到无穷大（请检查定义域）")
    return number


# ---------------------------------------------------------------------------
# 三、Formula：可复用的"已验证公式"对象
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 三、Desmos 风格公式预处理（v1.2）
# ---------------------------------------------------------------------------
# 用户可以直接用 Desmos 的写法：
#   2x^2 + sin(πx)、y = sin(x)、|x| + 1、√x、2√(x+1)、2(x+1)、pi x、x≤1 …
# 这里只做"文本→标准数学表达式"的翻译；真正的安全校验仍然是后面的 AST 白名单。
# 翻译结果不会引入任何新能力（只是补 * 号、换符号、去 y= 前缀、绝对值/根号转函数）。

_FULLWIDTH_TRANS = str.maketrans(
    {
        "×": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "—": "-",
        "π": "pi ", "τ": "tau ", "≤": "<=", "≥": ">=", "≠": "!=",
        "²": "^2", "³": "^3",
        "（": "(", "）": ")", "，": ",", "．": ".", "＝": "=",
        "＋": "+", "－": "-", "＊": "*", "／": "/", "｜": "|",
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    }
)

_SQRT_ATOM_RE = re.compile(r"√\s*(\([^()]*\)|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*)")
_EQUATION_RE = re.compile(r"(?<![<>=!])=(?!=)")
_LABEL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\s*\([^()]*\))?")

_PYTHON_KEYWORDS = frozenset({"if", "else", "and", "or", "not"})

_TOKEN_RE = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
    | (?P<qualified>math\.[A-Za-z_][A-Za-z0-9_]*)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>\*\*|<=|>=|==|!=|[+\-*/%<>,()])
    | (?P<other>.)
    """,
    re.VERBOSE,
)


def _strip_equation(text: str) -> str:
    """去掉 Desmos 风格的左边（y = / z = / r = / f(x) = …），只保留右边表达式。

    只认 Desmos "图像标签" 形式的左边；x = 1、t = … 这类会保持原样并报语法错误，
    避免把 "x = 1" 悄悄当成常数 1 生成错误几何。
    """
    hits = list(_EQUATION_RE.finditer(text))
    if len(hits) != 1:
        return text
    left = text[: hits[0].start()].strip()
    right = text[hits[0].end():].strip()
    if right and _is_equation_label(left):
        return right
    return text


def _is_equation_label(left: str) -> bool:
    """判断等号左边是不是 Desmos 的"图像标签"（y / z / r / y1 / f(x)）。"""
    if not left:
        return False
    if _LABEL_RE.fullmatch(left) and "(" not in left:
        if left in ("y", "z", "r"):
            return True
        return re.fullmatch(r"[A-Za-z]\d+", left) is not None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*\([^()]*\)", left):
        return True
    return False


def _expand_absolute_bars(text: str) -> str:
    """把成对的 |…| 转成 abs(…)：|x| + |y| -> abs(x) + abs(y)。"""
    if "|" not in text:
        return text
    if text.count("|") % 2 != 0:
        raise FormulaError("绝对值符号 | 没有配对，请写成 |x| 这种成对形式")
    parts = text.split("|")
    result = parts[0]
    for index in range(1, len(parts)):
        if index % 2 == 1:
            result += "abs(" + parts[index]
        else:
            result += ")" + parts[index]
    return result


def _translate_sqrt(text: str) -> str:
    """√x / √(x+1) / √2 -> sqrt(x) / sqrt(x+1) / sqrt(2)。"""
    text = _SQRT_ATOM_RE.sub(lambda m: "sqrt(" + m.group(1) + ")", text)
    if "√" in text:
        raise FormulaError("√ 后面需要跟数字、变量或括号，例如 √x 或 √(x+1)")
    return text


def _tokenize(text: str):
    tokens = []
    for match in _TOKEN_RE.finditer(text):
        kind = match.lastgroup
        value = match.group(0)
        if kind == "space":
            continue
        tokens.append([kind, value])
    return tokens


def _classify_tokens(tokens, variables):
    """标记每个 token 是数值/变量/函数/运算符，并把连写变量拆开（xy -> x*y）。"""
    variables = set(variables)
    result = []
    for kind, value in tokens:
        if kind == "ident":
            if value in _PYTHON_KEYWORDS:
                result.append(["keyword", value])
            elif value in variables or value in CONSTANTS:
                result.append(["value", value])
            elif value in FUNCTIONS:
                result.append(["func", value])
            elif value == "math":
                result.append(["math", value])
            elif len(value) > 1 and all(char in variables for char in value):
                for index, char in enumerate(value):
                    if index:
                        result.append(["op", "*"])
                    result.append(["value", char])
            elif len(value) > 1 and value[0] in variables and value[1:].isdigit():
                result.extend([["value", value[0]], ["op", "*"], ["number", value[1:]]])
            else:
                result.append(["ident", value])
        elif kind == "qualified":
            name = value.split(".", 1)[1]
            if name in FUNCTIONS:
                result.append(["func", value])
            elif name in CONSTANTS:
                result.append(["value", value])
            else:
                result.append(["ident", value])
        else:
            result.append([kind, value])
    return result


def _insert_implicit_multiplication(tokens):
    """按 Desmos 习惯补乘号：2x、2(x+1)、(x+1)(x-1)、2sin(x)、pi x …"""
    result = []
    for token in tokens:
        if result:
            prev_kind, prev_value = result[-1][0], result[-1][1]
            kind = token[0]
            prev_is_value = prev_kind in ("number", "value") or prev_value == ")"
            curr_is_value = kind in ("number", "value", "func") or token[1] == "("
            # "函数名 + (" 是调用，不是乘法
            is_call = prev_kind in ("func", "math") and kind == "("
            if prev_is_value and curr_is_value and not is_call:
                result.append(["op", "*"])
        result.append(token)
    return result


def preprocess_expression(source: str, variables=("x", "y")) -> str:
    """把 Desmos 风格输入翻译成 Python 可安全求值的标准表达式。"""
    text = source.strip()
    if not text:
        return text

    # 先把全角符号（（）、π、≤、数字等）转成 ASCII，再处理 √，否则 √（x）会漏右括号
    text = text.translate(_FULLWIDTH_TRANS)
    text = _translate_sqrt(text)
    text = _strip_equation(text)
    text = _expand_absolute_bars(text)
    # ^ 是 Desmos 的幂运算写法，Python 用 **
    text = re.sub(r"(?<!\*)\^(?!\*)", "**", text)

    tokens = _classify_tokens(_tokenize(text), variables)
    tokens = _insert_implicit_multiplication(tokens)
    parts = []
    for token in tokens:
        if token[0] == "keyword":
            parts.append(" " + token[1] + " ")
        else:
            parts.append(token[1])
    translated = re.sub(r"\s+", " ", "".join(parts)).strip()
    return translated


class Formula:
    """一个已经通过白名单校验、可以反复求值的数学公式。

    典型用法::

        f = Formula("sin(x) * 2")
        z = f.evaluate(1.0, 0.0)      # x=1.0, y=0.0
        f.evaluate(0.0, 2.0)          # x=0.0, y=2.0

    每次 evaluate 都会复制一份受限命名空间，因此绝不会污染 Blender 的全局环境。
    """

    __slots__ = (
        "source", "variables", "parameters", "parameter_defaults",
        "translated", "tree", "_code",
    )

    def __init__(
        self,
        source: str,
        variables=("x", "y"),
        parameters=(),
        parameter_defaults=None,
    ) -> None:
        self.variables = tuple(variables)
        self.parameters = tuple(parameters)
        self.parameter_defaults = dict(parameter_defaults or {})
        if not self.variables or len(set(self.variables)) != len(self.variables):
            raise FormulaError("变量名配置不合法")
        overlap = set(self.variables) & set(self.parameters)
        if overlap:
            raise FormulaError(f"变量与参数重名：{', '.join(sorted(overlap))}")
        self.source = _check_formula_basics(source)
        self.translated = preprocess_expression(self.source, self.variables + self.parameters)
        eval_source = self.translated or self.source
        try:
            self.tree = ast.parse(eval_source, mode="eval")
        except SyntaxError as exc:
            message = f"公式语法错误：{exc.msg}（第 {exc.offset} 列）"
            if self.translated != self.source:
                message += f"，按 Desmos 语法转换后为：{self.translated}"
            raise FormulaError(message) from None
        node_count = sum(1 for _ in ast.walk(self.tree))
        if node_count > MAX_AST_NODES:
            raise FormulaError(f"公式太复杂（AST 节点数 {node_count}，上限 {MAX_AST_NODES}）")
        try:
            _Validator(self.variables + self.parameters).visit(self.tree)
        except FormulaError as exc:
            if self.translated != self.source:
                raise FormulaError(f"{exc}（按 Desmos 语法转换后：{self.translated}）") from None
            raise
        try:
            # 编译已验证的语法树；来源字符串仅用于错误提示
            self._code = compile(self.tree, "<函数公式>", "eval")
        except (SyntaxError, ValueError, TypeError) as exc:  # 理论上不会发生
            raise FormulaError(f"公式无法编译：{exc}") from None

    # -- 求值 -------------------------------------------------------------
    def evaluate(self, *args, **kwargs) -> float:
        """求公式值。

        支持两种调用方式（兼容旧代码）：
            f.evaluate(1.0, 2.0)          # 按变量顺序（旧公式 x, y）
            f.evaluate(t=1.0)             # 按变量名（参数模式 t / u,v）
        失败时抛 FormulaError（带变量值，便于定位问题）。
        """
        if args:
            if kwargs or len(args) != len(self.variables):
                raise FormulaError(
                    f"公式变量是 {self.variables}，传入的值数量不对：{args!r}"
                )
            values = dict(zip(self.variables, args))
        else:
            missing = [name for name in self.variables if name not in kwargs]
            if missing:
                raise FormulaError(f"求值缺少变量：{', '.join(missing)}")
            values = dict(kwargs)
        # 系数参数 a/b/c/d：调用方没传时用默认值
        for name in self.parameters:
            if name in values:
                continue
            if name in self.parameter_defaults:
                values[name] = float(self.parameter_defaults[name])
            else:
                raise FormulaError(f"求值缺少参数：{name}")

        env = dict(_BASE_ENV)
        env.update(values)
        coordinates = ", ".join(f"{name}={values[name]:.6g}" for name in self.variables)
        try:
            value = eval(self._code, env)  # noqa: S307 - 已做 AST 白名单限制
        except FormulaError:
            raise
        except Exception as exc:  # ZeroDivisionError / math domain / OverflowError ...
            raise FormulaError(
                f"公式在 {coordinates} 处计算失败：{type(exc).__name__}: {exc}"
            ) from None
        return _to_float(value, coordinates=coordinates)

    def __repr__(self) -> str:  # 方便调试
        return f"Formula({self.source!r})"


def validate_text(source: str, variables=("x", "y")) -> None:
    """只校验不缓存（面板实时显示对/错用），非法时抛 FormulaError。"""
    Formula(source, variables, parameters=PARAMETERS,
            parameter_defaults={name: 1.0 for name in PARAMETERS})


# ---------------------------------------------------------------------------
# 四、采样工具（与 bpy 无关，可直接单元测试）
# ---------------------------------------------------------------------------

def _linspace(start: float, end: float, segments: int) -> List[float]:
    """把 [start, end] 等分成 segments 段，返回 segments+1 个点（含两端）。"""
    if segments < 1:
        raise FormulaError("分段数至少为 1")
    step = (end - start) / segments
    return [start + step * i for i in range(segments)] + [end]


def sample_1d(
    formula: Formula,
    start: float,
    end: float,
    segments: int,
) -> List[Tuple[float, float]]:
    """圆管模式的采样：沿参数 t 从 start 到 end 取 segments+1 个点。

    返回 [(t, z), ...]。几何上：t 会作为曲线的 Y 坐标（路径沿 Y 轴），
    z 是公式值（Z 轴高度）。为了让 sin(x) 和 cos(y) 两种写法都能用，
    求值时令 x = y = t（即 x 与 y 都是当前采样参数）。
    """
    points: List[Tuple[float, float]] = []
    for t in _linspace(start, end, segments):
        points.append((t, formula.evaluate(x=t, y=t)))
    return points


def sample_2d(
    formula: Formula,
    start: float,
    end: float,
    segments: int,
) -> List[List[float]]:
    """曲面模式的采样：返回按行（Y 从 start 到 end）组织的二维高度表。

    heights[j][i] 对应 y = values[j]、x = values[i] 的公式值。
    网格顶点数 = (segments+1)^2，与 Blender 网格顶点排列顺序配合见 generators.py。
    """
    values = _linspace(start, end, segments)
    heights: List[List[float]] = []
    for y in values:
        row = [formula.evaluate(x=x, y=y) for x in values]
        heights.append(row)
    return heights

# ---------------------------------------------------------------------------
# 五、参数方程采样（v1.1 新增）
# ---------------------------------------------------------------------------

PARAM_CURVE_VARIABLES = ("t",)
PARAM_SURFACE_VARIABLES = ("u", "v")


def sample_param_curve(
    formulas,
    start: float,
    end: float,
    segments: int,
) -> List[Tuple[float, float, float]]:
    """参数曲线采样：formulas = (x(t), y(t), z(t))，返回 segments+1 个空间点。"""
    if len(formulas) != 3:
        raise FormulaError("参数曲线需要 3 个公式：x(t)、y(t)、z(t)")
    points: List[Tuple[float, float, float]] = []
    for t in _linspace(start, end, segments):
        points.append(
            (
                formulas[0].evaluate(t=t),
                formulas[1].evaluate(t=t),
                formulas[2].evaluate(t=t),
            )
        )
    return points


def sample_param_surface(
    formulas,
    u_start: float,
    u_end: float,
    v_start: float,
    v_end: float,
    segments_u: int,
    segments_v: int,
) -> List[List[Tuple[float, float, float]]]:
    """参数曲面采样：formulas = (x(u,v), y(u,v), z(u,v))。

    返回按 v 行、u 列组织的三维点表：points[v_index][u_index]。
    顶点数 = (segments_u+1) * (segments_v+1)。
    """
    if len(formulas) != 3:
        raise FormulaError("参数曲面需要 3 个公式：x(u,v)、y(u,v)、z(u,v)")
    u_values = _linspace(u_start, u_end, segments_u)
    v_values = _linspace(v_start, v_end, segments_v)
    points: List[List[Tuple[float, float, float]]] = []
    for v in v_values:
        row = []
        for u in u_values:
            row.append(
                (
                    formulas[0].evaluate(u=u, v=v),
                    formulas[1].evaluate(u=u, v=v),
                    formulas[2].evaluate(u=u, v=v),
                )
            )
        points.append(row)
    return points