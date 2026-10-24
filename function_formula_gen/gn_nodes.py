# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""几何节点（Geometry Nodes）集成模块。

用户需求："界面位置：几何节点中新加入节点。"
实现思路
========
几何节点本身不支持字符串求值，所以本模块把用户的公式**编译成节点**（烘焙进节点组）：
    * 用 formula.Formula 做完安全校验后拿到 AST；
    * 把 AST 递归翻译成 Math 节点链（加减乘除、幂、三角、对数、比较、条件表达式……）；
    * 用 Grid（曲面）或 Mesh Line（圆管）节点生成采样顶点，
      再用 Set Position 写入公式算出的高度，最后 Set Shade Smooth 平滑；
    * 把这些节点包装成一个 GeometryNodeTree 节点组，接口暴露 范围/分辨率/半径，
      用户可以在修改器和节点组里继续调参。

入口
====
1. 几何节点编辑器的 Add 菜单 > "函数公式节点"（NODE_MT_geometry_node_add_all 里注册）；
2. 几何节点编辑器侧栏 "函数生成" 面板 > "插入节点到当前树" / "生成几何节点对象"；
3. 3D 视图侧栏 "几何节点版本" 面板 > "生成几何节点对象"。

维护提示
========
* 新增公式函数：在 _GN_SIMPLE_FUNCTIONS / _GN_SPECIAL_FUNCTIONS 里加一条映射即可，
  忘记加时这里会给出"几何节点版本暂不支持"的明确错误（静态生成不受影响）。
* 新增采样模式：在 _build_surface_body / _build_tube_body 旁边加一个 _build_xxx_body，
  并在 _BODY_BUILDERS 注册；接口 socket 名称在 _SOCKET_* 常量里统一维护。
* 节点组名字包含公式，便于用户区分；节点组自定义属性 _GROUP_PROP_* 用来做复用查找。
"""
from __future__ import annotations

import ast
import json
import math
from typing import Dict, List, Sequence, Union

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty

from . import formula as formula_module
from . import generators
from . import materials
from .formula import (
    PARAM_CURVE_VARIABLES,
    PARAM_SURFACE_VARIABLES,
    PARAMETERS,
    Formula,
    FormulaError,
)
from .generators import GenerationError
from .properties import (
    HEIGHT_MODES,
    MODE_ITEMS,
    PARAM_MODES,
    TUBE_MODES,
    FunctionGenSettings,
)

# ---------------------------------------------------------------------------
# 常量：接口 socket 名称 / 节点组标记
# ---------------------------------------------------------------------------

_SOCKET_START = "范围起始"
_SOCKET_END = "范围结束"
_SOCKET_RANGE2_START = "范围2起始"
_SOCKET_RANGE2_END = "范围2结束"
_SOCKET_RESOLUTION = "分辨率"
_SOCKET_RADIUS = "管道半径"
_SOCKET_OUTPUT = "几何体"
# v1.3 新外露的节点输入
_SOCKET_ADAPTIVE = "自适应加密"
_SOCKET_SMOOTH = "平滑着色"
_SOCKET_SCALE = "坐标缩放"
_SOCKET_OFFSET = "位置偏移"
_SOCKET_CIRCLE_RES = "截面边数"
_SOCKET_FILL_CAPS = "封口"
_SOCKET_ATTRS = "输出属性"
_SOCKET_WELD = "接缝焊接"
_SOCKET_MATERIAL = "材质"
_SOCKET_PARAMS = ("参数 a", "参数 b", "参数 c", "参数 d")
# 函数场节点
_SOCKET_POSITION_IN = "位置"
_SOCKET_FIELD_VALUE = "值"
_SOCKET_T = "参数 t"
_SOCKET_U = "参数 u"
_SOCKET_V = "参数 v"

_GROUP_PROP_MARKER = "functiongen_gn_group"
_GROUP_PROP_MODE = "functiongen_gn_mode"
_GROUP_PROP_FORMULA = "functiongen_gn_formula"
_GROUP_PROP_VERSION = "functiongen_gn_version"
_GROUP_PROP_FIELD = "functiongen_gn_field"
_GROUP_PROP_FIELD_KIND = "functiongen_gn_field_kind"
_GN_VERSION = 3

_FIELD_KINDS = ("SCALAR", "CURVE_VECTOR", "SURFACE_VECTOR")
_FIELD_LABELS = {
    "SCALAR": "函数值场（x,y→值）",
    "CURVE_VECTOR": "曲线向量场（t→xyz）",
    "SURFACE_VECTOR": "曲面向量场（u,v→xyz）",
}

_MODE_LABELS = {
    "TUBE": "圆管",
    "SURFACE": "曲面",
    "PARAM_CURVE": "参数曲线",
    "PARAM_SURFACE": "参数曲面",
}
# 几何变量集（系数 a/b/c/d 另由 _validate_sources 作为 parameters 传入）
_MODE_VARIABLES = {
    "TUBE": ("x", "y"),
    "SURFACE": ("x", "y"),
    "PARAM_CURVE": PARAM_CURVE_VARIABLES,
    "PARAM_SURFACE": PARAM_SURFACE_VARIABLES,
}

# 节点组里公式链的起点（x 坐标），避免和采样节点重叠
_FORMULA_X = 1200.0
_FORMULA_Y_STEP = -180.0

# 自动加密采样（与 generators.effective_segments 保持一致）
_REFERENCE_SPAN = 10.0
_MAX_CURVE_SEGMENTS = 8000.0
_MAX_SURFACE_SEGMENTS = 512.0

# ---------------------------------------------------------------------------
# 公式 AST -> Math 节点 的映射表
# ---------------------------------------------------------------------------

# 直接对应 Blender Math 节点 operation 的函数（第二项是参数个数）
_GN_SIMPLE_FUNCTIONS: Dict[str, tuple] = {
    "sin": ("SINE", 1),
    "cos": ("COSINE", 1),
    "tan": ("TANGENT", 1),
    "asin": ("ARCSINE", 1),
    "acos": ("ARCCOSINE", 1),
    "atan": ("ARCTANGENT", 1),
    "sinh": ("SINH", 1),
    "cosh": ("COSH", 1),
    "tanh": ("TANH", 1),
    "exp": ("EXPONENT", 1),
    "sqrt": ("SQRT", 1),
    "abs": ("ABSOLUTE", 1),
    "fabs": ("ABSOLUTE", 1),
    "floor": ("FLOOR", 1),
    "ceil": ("CEIL", 1),
    "round": ("ROUND", 1),
    "trunc": ("TRUNC", 1),
    "sign": ("SIGN", 1),
    "atan2": ("ARCTAN2", 2),
    "pow": ("POWER", 2),
    "fmod": ("MODULO", 2),
}

_GN_BINARY_OPS: Dict[type, str] = {
    ast.Add: "ADD",
    ast.Sub: "SUBTRACT",
    ast.Mult: "MULTIPLY",
    ast.Div: "DIVIDE",
    ast.Pow: "POWER",
    # Python 的 % 是"向下取整取模"，对应 Blender 的 FLOORED_MODULO
    ast.Mod: "FLOORED_MODULO",
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _new_node(tree, bl_idname: str, location=(0.0, 0.0), label: str = "") -> bpy.types.Node:
    """新建节点并放到指定位置（方便用户查看节点组内部结构）。"""
    node = tree.nodes.new(bl_idname)
    node.location = location
    if label:
        node.label = label
    return node


def _link(tree, output_socket, input_socket) -> None:
    """建立连线（带类型检查的简单包装，出错时异常信息更友好）。"""
    try:
        tree.links.new(output_socket, input_socket)
    except Exception as exc:  # 正常不会失败，失败说明节点结构变了
        raise FormulaError(f"几何节点内部连线失败：{exc}") from None


def _set_default(socket, value) -> None:
    try:
        socket.default_value = value
    except (TypeError, AttributeError):
        pass


def _input_socket(node: bpy.types.Node, name: str):
    """按名称找到一个输入 socket。

    注意：Blender 5.2 里有些"当前模式下不可用"的 socket（例如 Mesh Line 的
    Resolution）虽然会出现在 node.inputs 的迭代结果里，但用 node.inputs["名字"]
    这种 bpy_prop_collection 查找会抛 KeyError。所以统一用迭代方式按名字查找。
    """
    for socket in node.inputs:
        if socket.name == name:
            return socket
    raise FormulaError(f"节点 {node.bl_idname} 缺少输入：{name}")


def _output_socket(node: bpy.types.Node, name: str):
    """按名称找到一个输出 socket（同样绕开 collection 查找的坑）。"""
    for socket in node.outputs:
        if socket.name == name:
            return socket
    raise FormulaError(f"节点 {node.bl_idname} 缺少输出：{name}")


def _set_float(tree, socket, operand: Union[float, bpy.types.NodeSocket]) -> None:
    """把常量或 socket 接到一个 float 输入口上（常量直接写 default_value）。"""
    if isinstance(operand, (int, float)):
        _set_default(socket, float(operand))
    else:
        _link(tree, operand, socket)


class _GNCompiler:
    """把公式 AST 翻译成 Geometry Nodes 里的 Math 节点链。

    编译结果用两种形式表示：
    * float       —— 常量（会直接写进下游节点的 default_value，减少节点数量）；
    * NodeSocket  —— 节点输出（字段），可以继续连接。

    设计说明：常量可以在编译期折叠成 Python 计算结果，这样生成的节点图更干净，
    而且与"静态生成"用 Python 求值的结果完全一致。
    """

    def __init__(self, tree, variables: Dict[str, bpy.types.NodeSocket]):
        self.tree = tree
        self.variables = variables      # {"x": socket, "y": socket}
        self._counter = 0

    # -- 内部工具 ---------------------------------------------------------
    def _place(self):
        location = (_FORMULA_X, _FORMULA_Y_STEP * self._counter)
        self._counter += 1
        return location

    def _math(self, operation: str, a, b=None, c=None) -> bpy.types.NodeSocket:
        """创建一个 Math 节点；a/b/c 可以是常量或 socket。"""
        node = _new_node(self.tree, "ShaderNodeMath", self._place(), label=operation)
        node.operation = operation
        for index, operand in enumerate((a, b, c)):
            if operand is None:
                continue
            _set_float(self.tree, node.inputs[index], operand)
        return node.outputs[0]

    def _value(self, value: float) -> bpy.types.NodeSocket:
        node = _new_node(self.tree, "ShaderNodeValue", self._place(), label=f"{value:g}")
        node.outputs[0].default_value = float(value)
        return node.outputs[0]

    def _as_socket(self, operand) -> bpy.types.NodeSocket:
        return self._value(operand) if isinstance(operand, (int, float)) else operand

    # -- 主入口 -----------------------------------------------------------
    def compile(self, node: ast.AST):
        """递归编译一个 AST 节点，返回 float 或 NodeSocket。"""
        if isinstance(node, ast.Expression):
            return self.compile(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return self.variables[node.id]
            if node.id in formula_module.CONSTANTS:
                return float(formula_module.CONSTANTS[node.id])
            raise FormulaError(f"几何节点版本不认识变量：{node.id}")
        if isinstance(node, ast.Attribute):
            # 白名单校验已保证是 math.xxx，且 xxx 是常量
            if node.attr in formula_module.CONSTANTS:
                return float(formula_module.CONSTANTS[node.attr])
            raise FormulaError(f"几何节点版本不支持：math.{node.attr}")
        if isinstance(node, ast.UnaryOp):
            return self._compile_unary(node)
        if isinstance(node, ast.BinOp):
            return self._compile_binary(node)
        if isinstance(node, ast.Call):
            return self._compile_call(node)
        if isinstance(node, ast.Compare):
            return self._compile_compare(node)
        if isinstance(node, ast.BoolOp):
            return self._compile_bool_op(node)
        if isinstance(node, ast.IfExp):
            return self._compile_if_exp(node)
        raise FormulaError(f"几何节点版本暂不支持该语法：{type(node).__name__}")

    # -- 各类语法 ---------------------------------------------------------
    def _compile_unary(self, node: ast.UnaryOp):
        operand = self.compile(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            if isinstance(operand, (int, float)):
                return -operand
            return self._math("MULTIPLY", operand, -1.0)
        if isinstance(node.op, ast.Not):
            if isinstance(operand, (int, float)):
                return float(not operand)
            return self._math("SUBTRACT", 1.0, operand)
        raise FormulaError(f"几何节点版本不支持一元运算：{type(node.op).__name__}")

    def _compile_binary(self, node: ast.BinOp):
        left = self.compile(node.left)
        right = self.compile(node.right)
        # 常量折叠：两边都是常量时直接用 Python 算，与静态生成完全一致
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            try:
                if isinstance(node.op, ast.Div):
                    return float(left) / float(right)
                if isinstance(node.op, ast.FloorDiv):
                    return float(left) // float(right)
                if isinstance(node.op, ast.Pow):
                    return float(left) ** float(right)
                if isinstance(node.op, ast.Mod):
                    return float(left) % float(right)
                if isinstance(node.op, ast.Add):
                    return float(left) + float(right)
                if isinstance(node.op, ast.Sub):
                    return float(left) - float(right)
                if isinstance(node.op, ast.Mult):
                    return float(left) * float(right)
            except Exception as exc:
                raise FormulaError(f"常量计算失败：{exc}") from None
        if isinstance(node.op, ast.FloorDiv):
            # // 没有对应 operation：先除再向下取整
            return self._math("FLOOR", self._math("DIVIDE", left, right))
        operation = _GN_BINARY_OPS.get(type(node.op))
        if operation is None:
            raise FormulaError(f"几何节点版本不支持运算符：{type(node.op).__name__}")
        return self._math(operation, left, right)

    def _compile_call(self, node: ast.Call):
        # 函数名：可能是 Name(sin) 或 Attribute(math.sin)
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            raise FormulaError("不支持的函数调用形式")
        args = [self.compile(arg) for arg in node.args]
        all_const = all(isinstance(arg, (int, float)) for arg in args)

        # 常量参数：直接调 Python 的 math 函数（与静态生成一致）
        if all_const:
            python_func = formula_module.FUNCTIONS.get(name)
            if python_func is None:
                raise FormulaError(f"几何节点版本不支持函数：{name}")
            try:
                return float(python_func(*[float(arg) for arg in args]))
            except Exception as exc:
                raise FormulaError(f"{name}() 常量计算失败：{exc}") from None

        simple = _GN_SIMPLE_FUNCTIONS.get(name)
        if simple is not None:
            operation, arity = simple
            if len(args) != arity:
                raise FormulaError(f"{name}() 需要 {arity} 个参数，实际 {len(args)} 个")
            return self._math(operation, *args)

        if name in ("log", "ln"):
            if len(args) not in (1, 2):
                raise FormulaError("log() 只支持 log(x) 或 log(x, base)")
            base = args[1] if len(args) == 2 else math.e
            node = self._math("LOGARITHM", args[0])
            math_node = node.node
            _set_float(self.tree, math_node.inputs[1], base)
            return node
        if name in ("log2", "log10"):
            base = 2.0 if name == "log2" else 10.0
            node = self._math("LOGARITHM", args[0])
            _set_float(self.tree, node.node.inputs[1], base)
            return node
        if name == "hypot":
            if len(args) != 2:
                raise FormulaError("hypot() 需要 2 个参数")
            squared = self._math("ADD", self._math("MULTIPLY", args[0], args[0]),
                                 self._math("MULTIPLY", args[1], args[1]))
            return self._math("SQRT", squared)
        if name in ("min", "max"):
            if not args:
                raise FormulaError(f"{name}() 至少需要 1 个参数")
            operation = "MINIMUM" if name == "min" else "MAXIMUM"
            result = self._as_socket(args[0])
            for arg in args[1:]:
                result = self._math(operation, result, arg)
            return result
        if name == "clamp":
            if len(args) != 3:
                raise FormulaError("clamp() 需要 3 个参数：clamp(值, 最小, 最大)")
            return self._math("MINIMUM",
                              self._math("MAXIMUM", args[0], args[1]), args[2])
        if name == "degrees":
            if len(args) != 1:
                raise FormulaError("degrees() 需要 1 个参数")
            return self._math("MULTIPLY", args[0], 180.0 / math.pi)
        if name == "radians":
            if len(args) != 1:
                raise FormulaError("radians() 需要 1 个参数")
            return self._math("MULTIPLY", args[0], math.pi / 180.0)
        raise FormulaError(f"几何节点版本暂不支持函数：{name}()（静态生成仍然可用）")

    def _compare_pair(self, op, left, right):
        """单个比较运算，返回 0/1 的 float 或 socket。"""
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            python_ops = {
                ast.Lt: lambda a, b: a < b,
                ast.LtE: lambda a, b: a <= b,
                ast.Gt: lambda a, b: a > b,
                ast.GtE: lambda a, b: a >= b,
                ast.Eq: lambda a, b: a == b,
                ast.NotEq: lambda a, b: a != b,
            }
            if type(op) in python_ops:
                return float(python_ops[type(op)](float(left), float(right)))
        if isinstance(op, ast.Lt):
            return self._math("LESS_THAN", left, right)
        if isinstance(op, ast.Gt):
            return self._math("GREATER_THAN", left, right)
        if isinstance(op, ast.LtE):
            return self._math("SUBTRACT", 1.0, self._math("GREATER_THAN", left, right))
        if isinstance(op, ast.GtE):
            return self._math("SUBTRACT", 1.0, self._math("LESS_THAN", left, right))
        if isinstance(op, ast.Eq):
            return self._math("COMPARE", left, right, 1e-9)
        if isinstance(op, ast.NotEq):
            return self._math("SUBTRACT", 1.0, self._math("COMPARE", left, right, 1e-9))
        raise FormulaError(f"几何节点版本不支持比较运算：{type(op).__name__}")

    def _compile_compare(self, node: ast.Compare):
        values = [self.compile(node.left)] + [self.compile(item) for item in node.comparators]
        # 链式比较 a < b < c 等价于 (a<b) 且 (b<c)：用乘法当逻辑与
        result = self._compare_pair(node.ops[0], values[0], values[1])
        for index, op in enumerate(node.ops[1:], start=1):
            pair = self._compare_pair(op, values[index], values[index + 1])
            if isinstance(result, (int, float)) and isinstance(pair, (int, float)):
                result = float(bool(result) and bool(pair))
            else:
                result = self._math("MULTIPLY", result, pair)
        return result

    def _compile_bool_op(self, node: ast.BoolOp):
        values = [self.compile(value) for value in node.values]
        is_and = isinstance(node.op, ast.And)
        result = values[0]
        for value in values[1:]:
            if isinstance(result, (int, float)) and isinstance(value, (int, float)):
                result = float(bool(result) and bool(value)) if is_and else float(bool(result) or bool(value))
            elif is_and:
                result = self._math("MULTIPLY", result, value)
            else:
                # a or b = a + b - a*b（对 0/1 数值成立）
                result = self._math("SUBTRACT",
                                    self._math("ADD", result, value),
                                    self._math("MULTIPLY", result, value))
        return result

    def _compile_if_exp(self, node: ast.IfExp):
        condition = self.compile(node.test)
        if isinstance(condition, (int, float)):
            return self.compile(node.body if condition else node.orelse)
        body = self.compile(node.body)
        orelse = self.compile(node.orelse)
        # result = orelse + (body - orelse) * condition
        difference = self._math("SUBTRACT", body, orelse)
        return self._math("ADD", orelse, self._math("MULTIPLY", difference, condition))


# ---------------------------------------------------------------------------
# 节点组创建 / 内部几何图
# ---------------------------------------------------------------------------

def _create_interface(group: bpy.types.NodeTree, mode: str = "SURFACE") -> None:
    """创建节点组接口（v1.3 扩展）。

    除了范围/分辨率/半径，还把以前烘焙死的选项外露成节点输入：
    自适应加密、平滑着色、坐标缩放、位置偏移、截面边数、封口、接缝焊接、输出属性，
    以及公式系数 参数 a/b/c/d。
    """
    specs = [
        (_SOCKET_START, "NodeSocketFloat", -5.0),
        (_SOCKET_END, "NodeSocketFloat", 5.0),
    ]
    if mode == "PARAM_SURFACE":
        specs.extend(
            [
                (_SOCKET_RANGE2_START, "NodeSocketFloat", -5.0),
                (_SOCKET_RANGE2_END, "NodeSocketFloat", 5.0),
            ]
        )
    specs.append((_SOCKET_RESOLUTION, "NodeSocketInt", 100))
    if mode in TUBE_MODES:
        specs.extend(
            [
                (_SOCKET_RADIUS, "NodeSocketFloat", 0.1),
                (_SOCKET_CIRCLE_RES, "NodeSocketInt", 16),
                (_SOCKET_FILL_CAPS, "NodeSocketBool", True),
            ]
        )
    specs.extend(
        [
            (_SOCKET_ADAPTIVE, "NodeSocketBool", True),
            (_SOCKET_SMOOTH, "NodeSocketBool", True),
            (_SOCKET_ATTRS, "NodeSocketBool", True),
            (_SOCKET_SCALE, "NodeSocketVector", (1.0, 1.0, 1.0)),
            (_SOCKET_OFFSET, "NodeSocketVector", (0.0, 0.0, 0.0)),
        ]
    )
    if mode == "PARAM_SURFACE":
        specs.append((_SOCKET_WELD, "NodeSocketBool", True))
    for name in _SOCKET_PARAMS:
        specs.append((name, "NodeSocketFloat", 1.0))
    specs.append((_SOCKET_MATERIAL, "NodeSocketMaterial", None))
    for name, socket_type, default in specs:
        item = group.interface.new_socket(name=name, in_out="INPUT", socket_type=socket_type)
        try:
            item.default_value = default
        except (AttributeError, TypeError):
            pass
    group.interface.new_socket(name=_SOCKET_OUTPUT, in_out="OUTPUT", socket_type="NodeSocketGeometry")


def _group_input(group):
    return _new_node(group, "NodeGroupInput", (-900.0, 0.0), label="参数输入")


def _group_output(group):
    return _new_node(group, "NodeGroupOutput", (3600.0, 0.0), label="几何体输出")


def _math_node(tree, operation: str, a=None, b=None, location=(0.0, 0.0), label: str = ""):
    """创建 Math 节点并返回输出 socket（a/b 可为 socket 或常量）。"""
    node = _new_node(tree, "ShaderNodeMath", location, label=label or operation)
    node.operation = operation
    for index, operand in enumerate((a, b)):
        if operand is None:
            continue
        _set_float(tree, node.inputs[index], operand)
    return node.outputs[0]


def _auto_resolution_socket(
    tree,
    input_node,
    span_start_socket,
    span_end_socket,
    cap: float,
    max_segment_length: float = None,
    resolution_socket=None,
):
    """几何节点里的"自动加密采样"：与静态生成器 effective_segments 规则一致。

    返回 分段数 socket：
        max(用户分段数, round(用户分段数 × 范围 / 10))，再用 cap 限制自动加密部分
        （用户自己设置的分段数超过 cap 时仍尊重用户值）。

    max_segment_length：曲线折线路径的最小采样密度（每段不超过该长度）。
    几何节点的圆管路径是折线（Mesh to Curve 之后），采样太稀会与公式曲线有可见偏差；
    静态生成器用 Bezier 平滑所以不需要，几何节点版本用它兜底保证精度。
    """
    if resolution_socket is None:
        resolution_socket = input_node.outputs[_SOCKET_RESOLUTION]
    base = _math_node(tree, "SUBTRACT", span_end_socket, span_start_socket, (-1400.0, -420.0), "范围跨度")
    ratio = _math_node(tree, "DIVIDE", base, _REFERENCE_SPAN, (-1240.0, -420.0), "跨度/10")
    scaled = _math_node(tree, "MULTIPLY", resolution_socket, ratio, (-1080.0, -420.0), "按比例加密")
    rounded = _math_node(tree, "ROUND", scaled, None, (-960.0, -420.0), "取整")
    maxed = _math_node(tree, "MAXIMUM", resolution_socket, rounded, (-840.0, -420.0), "不低于用户值")
    capped = _math_node(tree, "MINIMUM", maxed, cap, (-720.0, -420.0), "加密上限")
    result = _math_node(tree, "MAXIMUM", capped, resolution_socket, (-600.0, -420.0), "尊重用户高分辨率")
    if max_segment_length:
        density = _math_node(tree, "DIVIDE", base, max_segment_length, (-1080.0, -560.0), "最小采样密度")
        density = _math_node(tree, "CEIL", density, None, (-960.0, -560.0), "取整")
        result = _math_node(tree, "MAXIMUM", result, density, (-600.0, -560.0), "折线路径精度下限")
    return result


def _resolution_socket(
    tree,
    input_node,
    span_start_socket,
    span_end_socket,
    cap: float,
    max_segment_length: float = None,
):
    """实际分段数：自动加密开 -> 加密值，关 -> 用户输入值。"""
    auto = _auto_resolution_socket(
        tree, input_node, span_start_socket, span_end_socket, cap,
        max_segment_length=max_segment_length,
    )
    switch = _new_node(tree, "GeometryNodeSwitch", (-1400.0, -720.0), label="按开关选择采样密度")
    # 用 FLOAT 开关再送给 Mesh Line 的 Count（INT 插口会自动取整）；
    # 若直接把 FLOAT 接进 INT 开关的 True 口，Blender 会把它当作无效链接导致 0 顶点。
    switch.input_type = "FLOAT"
    _link(tree, input_node.outputs[_SOCKET_ADAPTIVE], _input_socket(switch, "Switch"))
    _link(tree, input_node.outputs[_SOCKET_RESOLUTION], _input_socket(switch, "False"))
    _link(tree, auto, _input_socket(switch, "True"))
    return switch.outputs[0]


def _bool_switch(tree, condition_socket, false_socket, true_socket, location, label: str):
    """布尔开关：condition 为真选 true，否则选 false。"""
    switch = _new_node(tree, "GeometryNodeSwitch", location, label=label)
    switch.input_type = "GEOMETRY"
    _link(tree, condition_socket, _input_socket(switch, "Switch"))
    _link(tree, false_socket, _input_socket(switch, "False"))
    _link(tree, true_socket, _input_socket(switch, "True"))
    return switch.outputs[0]


def _store_float_attribute(tree, geometry_socket, name: str, value_socket, location):
    """存一个 float 命名属性（点域），供下游几何节点/材质读取。"""
    store = _new_node(tree, "GeometryNodeStoreNamedAttribute", location, label=f"属性 {name}")
    store.data_type = "FLOAT"
    store.domain = "POINT"
    _link(tree, geometry_socket, _input_socket(store, "Geometry"))
    _set_default(_input_socket(store, "Name"), name)
    _link(tree, value_socket, _input_socket(store, "Value"))
    return store.outputs[0]


def _optional_attributes(tree, geometry_socket, input_node, attributes, location):
    """按「输出属性」开关决定是否写入函数值/参数属性。"""
    if not attributes:
        return geometry_socket
    stored = geometry_socket
    for index, (name, value_socket) in enumerate(attributes):
        stored = _store_float_attribute(
            tree, stored, name, value_socket,
            (location[0], location[1] - 160.0 * index),
        )
    return _bool_switch(
        tree, input_node.outputs[_SOCKET_ATTRS], geometry_socket, stored,
        (location[0] + 220.0, location[1]), "输出属性开关",
    )


def _transform_geometry(tree, geometry_socket, input_node, output_node, start_x: float = 2600.0):
    """坐标缩放 + 位置偏移（作用于最终几何体），再平滑着色并输出。"""
    position = _new_node(tree, "GeometryNodeInputPosition", (start_x - 500.0, 200.0), label="位置")
    multiply = _new_node(tree, "ShaderNodeVectorMath", (start_x - 320.0, 200.0), label="坐标缩放")
    multiply.operation = "MULTIPLY"
    _link(tree, position.outputs[0], multiply.inputs[0])
    _link(tree, input_node.outputs[_SOCKET_SCALE], multiply.inputs[1])
    add = _new_node(tree, "ShaderNodeVectorMath", (start_x - 140.0, 200.0), label="位置偏移")
    add.operation = "ADD"
    _link(tree, multiply.outputs[0], add.inputs[0])
    _link(tree, input_node.outputs[_SOCKET_OFFSET], add.inputs[1])
    set_position = _new_node(tree, "GeometryNodeSetPosition", (start_x + 40.0, 0.0), label="应用缩放/偏移")
    _link(tree, geometry_socket, _input_socket(set_position, "Geometry"))
    _link(tree, add.outputs[0], _input_socket(set_position, "Position"))
    geometry = _apply_material(tree, set_position.outputs[0], input_node, start_x + 240.0)
    _finish_shading_and_output(tree, geometry, output_node, input_node)


def _apply_material(tree, geometry_socket, input_node, x: float):
    """把节点组输入的「材质」写到几何体上（没有设置时保持空）。"""
    set_material = _new_node(tree, "GeometryNodeSetMaterial", (x, -40.0), label="设置材质")
    _link(tree, geometry_socket, _input_socket(set_material, "Geometry"))
    _link(tree, input_node.outputs[_SOCKET_MATERIAL], _input_socket(set_material, "Material"))
    return set_material.outputs[0]


def _sample_count(tree, input_node, resolution_socket=None):
    """采样点数 = 分段数 + 1（与静态生成器一致：分辨率表示"分段数"）。

    至少 2 个点，防止 Grid / Mesh Line 生成空几何。
    """
    if resolution_socket is None:
        resolution_socket = input_node.outputs[_SOCKET_RESOLUTION]
    clamped = _math_node(tree, "MAXIMUM", resolution_socket, 1.0, (-460.0, -260.0), "分段数≥1")
    count = _math_node(tree, "ADD", clamped, 1.0, (-300.0, -260.0), "顶点数=分段数+1")
    return count


def _map_range(tree, location, value_socket, to_min, to_max, from_min=0.0, from_max=1.0):
    """方便创建 Map Range 节点（float 版本，索引 0..4）。"""
    node = _new_node(tree, "ShaderNodeMapRange", location, label="范围映射")
    node.clamp = True
    _set_float(tree, node.inputs[0], value_socket)
    _set_float(tree, node.inputs[1], from_min)
    _set_float(tree, node.inputs[2], from_max)
    _set_float(tree, node.inputs[3], to_min)
    _set_float(tree, node.inputs[4], to_max)
    return node.outputs[0]


def _finish_shading_and_output(tree, geometry_socket, output_node, input_node=None):
    """Set Shade Smooth（可开关）-> Group Output。"""
    shade = _new_node(tree, "GeometryNodeSetShadeSmooth", (3200.0, 0.0), label="平滑着色")
    _set_float(tree, _input_socket(shade, "Mesh"), geometry_socket)
    if input_node is not None:
        _link(tree, input_node.outputs[_SOCKET_SMOOTH], _input_socket(shade, "Shade Smooth"))
    else:
        _set_default(_input_socket(shade, "Shade Smooth"), True)
    _link(tree, shade.outputs[0], output_node.inputs[0])


def _compiler_variables(input_node, variables):
    """编译公式时的可用变量：几何变量 + 系数参数 a/b/c/d 的节点输入。"""
    mapping = dict(variables)
    for name, socket_name in zip(PARAMETERS, _SOCKET_PARAMS):
        mapping[name] = input_node.outputs[socket_name]
    return mapping


def _build_surface_body(group, formulas: Sequence[Formula], input_node, output_node) -> None:
    """曲面：Grid -> 公式高度 -> 存函数值属性 -> Set Position -> 缩放/偏移/平滑。"""
    formula = formulas[0]
    resolution = _sample_count(
        group, input_node,
        _resolution_socket(
            group, input_node, input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
            _MAX_SURFACE_SEGMENTS,
        ),
    )

    grid = _new_node(group, "GeometryNodeMeshGrid", (-500.0, 0.0), label="采样网格")
    _set_default(_input_socket(grid, "Size X"), 1.0)
    _set_default(_input_socket(grid, "Size Y"), 1.0)
    _link(group, resolution, _input_socket(grid, "Vertices X"))
    _link(group, resolution, _input_socket(grid, "Vertices Y"))

    position = _new_node(group, "GeometryNodeInputPosition", (-500.0, -300.0), label="位置")
    separate = _new_node(group, "ShaderNodeSeparateXYZ", (-280.0, -300.0), label="拆分 XYZ")
    _link(group, position.outputs[0], separate.inputs[0])

    x_socket = _map_range(
        group, (-60.0, 220.0), separate.outputs[0],
        input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
        from_min=-0.5, from_max=0.5,
    )
    y_socket = _map_range(
        group, (-60.0, -320.0), separate.outputs[1],
        input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
        from_min=-0.5, from_max=0.5,
    )

    compiler = _GNCompiler(group, _compiler_variables(input_node, {"x": x_socket, "y": y_socket}))
    z_operand = compiler.compile(formula.tree)
    z_socket = compiler._as_socket(z_operand)

    geometry = _optional_attributes(
        group, grid.outputs[0], input_node,
        [("函数值", z_socket)],
        (900.0, 500.0),
    )

    combine = _new_node(group, "ShaderNodeCombineXYZ", (1500.0, 0.0), label="组合位置")
    _link(group, x_socket, combine.inputs[0])
    _link(group, y_socket, combine.inputs[1])
    _link(group, z_socket, combine.inputs[2])

    set_position = _new_node(group, "GeometryNodeSetPosition", (1800.0, 0.0), label="写入高度")
    _link(group, geometry, _input_socket(set_position, "Geometry"))
    _link(group, combine.outputs[0], _input_socket(set_position, "Position"))

    _transform_geometry(group, set_position.outputs[0], input_node, output_node)


def _build_tube_body(group, formulas: Sequence[Formula], input_node, output_node) -> None:
    """圆管：Mesh Line -> 公式高度 -> 参数/函数值属性 -> 曲线 -> 圆管 -> 缩放/偏移/平滑。"""
    formula = formulas[0]
    resolution = _sample_count(
        group, input_node,
        _resolution_socket(
            group, input_node, input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
            _MAX_CURVE_SEGMENTS, max_segment_length=0.1,
        ),
    )

    line = _new_node(group, "GeometryNodeMeshLine", (-500.0, 0.0), label="路径采样点")
    _link(group, resolution, _input_socket(line, "Count"))
    _set_default(_input_socket(line, "Resolution"), 1.0)
    _set_default(_input_socket(line, "Start Location"), (0.0, 0.0, 0.0))
    _set_default(_input_socket(line, "Offset"), (0.0, 1.0, 0.0))

    position = _new_node(group, "GeometryNodeInputPosition", (-500.0, -300.0), label="位置")
    separate = _new_node(group, "ShaderNodeSeparateXYZ", (-280.0, -300.0), label="拆分 XYZ")
    _link(group, position.outputs[0], separate.inputs[0])

    resolution_minus_one = _new_node(group, "ShaderNodeMath", (-280.0, -520.0), label="点数-1")
    resolution_minus_one.operation = "SUBTRACT"
    _set_float(group, resolution_minus_one.inputs[0], resolution)
    _set_float(group, resolution_minus_one.inputs[1], 1.0)

    t_socket = _map_range(
        group, (-60.0, -300.0), separate.outputs[1],
        input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
        from_min=0.0, from_max=resolution_minus_one.outputs[0],
    )

    compiler = _GNCompiler(group, _compiler_variables(input_node, {"x": t_socket, "y": t_socket}))
    z_operand = compiler.compile(formula.tree)
    z_socket = compiler._as_socket(z_operand)

    geometry = _optional_attributes(
        group, line.outputs[0], input_node,
        [("参数", t_socket), ("函数值", z_socket)],
        (900.0, 500.0),
    )

    combine = _new_node(group, "ShaderNodeCombineXYZ", (1500.0, 0.0), label="组合位置")
    _set_default(combine.inputs[0], 0.0)
    _link(group, t_socket, combine.inputs[1])
    _link(group, z_socket, combine.inputs[2])

    set_position = _new_node(group, "GeometryNodeSetPosition", (1800.0, 0.0), label="写入路径位置")
    _link(group, geometry, _input_socket(set_position, "Geometry"))
    _link(group, combine.outputs[0], _input_socket(set_position, "Position"))

    to_curve = _new_node(group, "GeometryNodeMeshToCurve", (2050.0, 0.0), label="网格转曲线")
    _link(group, set_position.outputs[0], to_curve.inputs[0])

    circle = _new_node(group, "GeometryNodeCurvePrimitiveCircle", (2050.0, -320.0), label="圆管截面")
    _link(group, input_node.outputs[_SOCKET_CIRCLE_RES], _input_socket(circle, "Resolution"))
    _set_default(_input_socket(circle, "Radius"), 1.0)

    to_mesh = _new_node(group, "GeometryNodeCurveToMesh", (2350.0, 0.0), label="曲线转圆管")
    _link(group, to_curve.outputs[0], _input_socket(to_mesh, "Curve"))
    _link(group, circle.outputs[0], _input_socket(to_mesh, "Profile Curve"))
    _link(group, input_node.outputs[_SOCKET_RADIUS], _input_socket(to_mesh, "Scale"))
    _link(group, input_node.outputs[_SOCKET_FILL_CAPS], _input_socket(to_mesh, "Fill Caps"))

    _transform_geometry(group, to_mesh.outputs[0], input_node, output_node)


def _build_param_curve_body(group, formulas: Sequence[Formula], input_node, output_node) -> None:
    """参数曲线：Mesh Line(t) -> x(t),y(t),z(t) -> 参数属性 -> 圆管 -> 缩放/偏移/平滑。"""
    resolution = _sample_count(
        group, input_node,
        _resolution_socket(
            group, input_node, input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
            _MAX_CURVE_SEGMENTS, max_segment_length=0.1,
        ),
    )

    line = _new_node(group, "GeometryNodeMeshLine", (-500.0, 0.0), label="参数采样点")
    _link(group, resolution, _input_socket(line, "Count"))
    _set_default(_input_socket(line, "Resolution"), 1.0)
    _set_default(_input_socket(line, "Start Location"), (0.0, 0.0, 0.0))
    _set_default(_input_socket(line, "Offset"), (0.0, 1.0, 0.0))

    position = _new_node(group, "GeometryNodeInputPosition", (-500.0, -300.0), label="位置")
    separate = _new_node(group, "ShaderNodeSeparateXYZ", (-280.0, -300.0), label="拆分 XYZ")
    _link(group, position.outputs[0], separate.inputs[0])

    count_minus_one = _new_node(group, "ShaderNodeMath", (-280.0, -520.0), label="点数-1")
    count_minus_one.operation = "SUBTRACT"
    _set_float(group, count_minus_one.inputs[0], resolution)
    _set_float(group, count_minus_one.inputs[1], 1.0)

    t_socket = _map_range(
        group, (-60.0, -300.0), separate.outputs[1],
        input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
        from_min=0.0, from_max=count_minus_one.outputs[0],
    )

    compiler = _GNCompiler(group, _compiler_variables(input_node, {"t": t_socket}))
    x_operand = compiler.compile(formulas[0].tree)
    y_operand = compiler.compile(formulas[1].tree)
    z_operand = compiler.compile(formulas[2].tree)

    geometry = _optional_attributes(
        group, line.outputs[0], input_node,
        [("参数", t_socket)],
        (900.0, 500.0),
    )

    combine = _new_node(group, "ShaderNodeCombineXYZ", (1500.0, 0.0), label="组合位置")
    _link(group, compiler._as_socket(x_operand), combine.inputs[0])
    _link(group, compiler._as_socket(y_operand), combine.inputs[1])
    _link(group, compiler._as_socket(z_operand), combine.inputs[2])

    set_position = _new_node(group, "GeometryNodeSetPosition", (1800.0, 0.0), label="写入空间位置")
    _link(group, geometry, _input_socket(set_position, "Geometry"))
    _link(group, combine.outputs[0], _input_socket(set_position, "Position"))

    to_curve = _new_node(group, "GeometryNodeMeshToCurve", (2050.0, 0.0), label="网格转曲线")
    _link(group, set_position.outputs[0], to_curve.inputs[0])

    circle = _new_node(group, "GeometryNodeCurvePrimitiveCircle", (2050.0, -320.0), label="圆管截面")
    _link(group, input_node.outputs[_SOCKET_CIRCLE_RES], _input_socket(circle, "Resolution"))
    _set_default(_input_socket(circle, "Radius"), 1.0)

    to_mesh = _new_node(group, "GeometryNodeCurveToMesh", (2350.0, 0.0), label="曲线转圆管")
    _link(group, to_curve.outputs[0], _input_socket(to_mesh, "Curve"))
    _link(group, circle.outputs[0], _input_socket(to_mesh, "Profile Curve"))
    _link(group, input_node.outputs[_SOCKET_RADIUS], _input_socket(to_mesh, "Scale"))
    _link(group, input_node.outputs[_SOCKET_FILL_CAPS], _input_socket(to_mesh, "Fill Caps"))

    _transform_geometry(group, to_mesh.outputs[0], input_node, output_node)


def _build_param_surface_body(group, formulas: Sequence[Formula], input_node, output_node) -> None:
    """参数曲面：Grid(u,v) -> x,y,z(u,v) -> 参数属性 -> 焊接开关 -> 缩放/偏移/平滑。"""
    resolution = _sample_count(
        group, input_node,
        _resolution_socket(
            group, input_node, input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
            _MAX_SURFACE_SEGMENTS,
        ),
    )

    grid = _new_node(group, "GeometryNodeMeshGrid", (-500.0, 0.0), label="参数采样网格")
    _set_default(_input_socket(grid, "Size X"), 1.0)
    _set_default(_input_socket(grid, "Size Y"), 1.0)
    _link(group, resolution, _input_socket(grid, "Vertices X"))
    _link(group, resolution, _input_socket(grid, "Vertices Y"))

    position = _new_node(group, "GeometryNodeInputPosition", (-500.0, -300.0), label="位置")
    separate = _new_node(group, "ShaderNodeSeparateXYZ", (-280.0, -300.0), label="拆分 XYZ")
    _link(group, position.outputs[0], separate.inputs[0])

    u_socket = _map_range(
        group, (-60.0, 220.0), separate.outputs[0],
        input_node.outputs[_SOCKET_START], input_node.outputs[_SOCKET_END],
        from_min=-0.5, from_max=0.5,
    )
    v_socket = _map_range(
        group, (-60.0, -320.0), separate.outputs[1],
        input_node.outputs[_SOCKET_RANGE2_START], input_node.outputs[_SOCKET_RANGE2_END],
        from_min=-0.5, from_max=0.5,
    )

    compiler = _GNCompiler(group, _compiler_variables(input_node, {"u": u_socket, "v": v_socket}))
    x_operand = compiler.compile(formulas[0].tree)
    y_operand = compiler.compile(formulas[1].tree)
    z_operand = compiler.compile(formulas[2].tree)

    geometry = _optional_attributes(
        group, grid.outputs[0], input_node,
        [("参数 U", u_socket), ("参数 V", v_socket)],
        (900.0, 500.0),
    )

    combine = _new_node(group, "ShaderNodeCombineXYZ", (1500.0, 0.0), label="组合位置")
    _link(group, compiler._as_socket(x_operand), combine.inputs[0])
    _link(group, compiler._as_socket(y_operand), combine.inputs[1])
    _link(group, compiler._as_socket(z_operand), combine.inputs[2])

    set_position = _new_node(group, "GeometryNodeSetPosition", (1800.0, 0.0), label="写入空间位置")
    _link(group, geometry, _input_socket(set_position, "Geometry"))
    _link(group, combine.outputs[0], _input_socket(set_position, "Position"))

    weld = _new_node(group, "GeometryNodeMergeByDistance", (2050.0, 0.0), label="焊接接缝")
    _link(group, set_position.outputs[0], _input_socket(weld, "Geometry"))
    _set_default(_input_socket(weld, "Distance"), 1e-6)
    geometry = _bool_switch(
        group, input_node.outputs[_SOCKET_WELD],
        set_position.outputs[0], weld.outputs[0],
        (2250.0, -220.0), "接缝焊接开关",
    )

    _transform_geometry(group, geometry, input_node, output_node)


_BODY_BUILDERS = {
    "SURFACE": _build_surface_body,
    "TUBE": _build_tube_body,
    "PARAM_CURVE": _build_param_curve_body,
    "PARAM_SURFACE": _build_param_surface_body,
}


def _short_formula(source: str, limit: int = 26) -> str:
    text = " ".join(str(source).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _normalize_sources(mode: str, sources) -> List[str]:
    """把 sources 统一成列表：高度场模式 1 个公式，参数模式 3 个公式。"""
    if isinstance(sources, str):
        sources = [sources]
    sources = [str(item) for item in sources]
    if mode in PARAM_MODES:
        if len(sources) != 3:
            raise GenerationError("参数模式需要 3 个公式（x/y/z 或 x/y/z(u,v)）")
    elif len(sources) != 1:
        raise GenerationError("该模式只需要 1 个公式")
    return sources


def _validate_sources(mode: str, sources: Sequence[str]) -> List[Formula]:
    variables = _MODE_VARIABLES.get(mode, ("x", "y"))
    defaults = {name: 1.0 for name in PARAMETERS}
    return [
        Formula(source, variables, parameters=PARAMETERS, parameter_defaults=defaults)
        for source in sources
    ]


def _group_name(mode: str, sources: Sequence[str]) -> str:
    label = _MODE_LABELS.get(mode, mode)
    first = _short_formula(sources[0], 18)
    suffix = " +…" if len(sources) > 1 else ""
    return f"函数公式·{label} [{first}{suffix}]"


def build_formula_group(mode: str, sources, group=None) -> bpy.types.NodeTree:
    """创建（或重建）一个"公式已烘焙"的几何节点组。

    sources：高度场模式传 1 个公式字符串；参数模式传 3 个公式
    （PARAM_CURVE 是 x(t),y(t),z(t)；PARAM_SURFACE 是 x(u,v),y(u,v),z(u,v)）。
    """
    sources = _normalize_sources(mode, sources)
    formulas = _validate_sources(mode, sources)
    builder = _BODY_BUILDERS.get(mode)
    if builder is None:
        raise GenerationError(f"几何节点版本不支持模式：{mode}")

    if group is None:
        group = bpy.data.node_groups.new(_group_name(mode, sources), "GeometryNodeTree")
    else:
        try:
            group.name = _group_name(mode, sources)
        except Exception:
            pass
    # 标记，便于查找复用（公式用 JSON 存，参数模式有 3 条）
    group[_GROUP_PROP_MARKER] = True
    group[_GROUP_PROP_MODE] = mode
    group[_GROUP_PROP_FORMULA] = json.dumps(sources, ensure_ascii=False)
    group[_GROUP_PROP_VERSION] = _GN_VERSION
    group.description = (
        f"由函数公式生成：{' / '.join(sources)}\n"
        f"模式：{_MODE_LABELS.get(mode, mode)}\n"
        "输入：范围/分辨率/半径/自适应加密/平滑着色/坐标缩放/位置偏移/"
        "截面边数/封口/接缝焊接/输出属性/参数 a-d\n"
        "参数 a-d 可以连接噪声、场景时间、编号等字段来让系数随空间或时间变化"
    )

    if len(group.interface.items_tree) == 0:
        _create_interface(group, mode)

    group.nodes.clear()                            # 重建内部节点（保留接口）
    input_node = _group_input(group)
    output_node = _group_output(group)
    builder(group, formulas, input_node, output_node)
    return group


def find_formula_group(mode: str, sources):
    """按"模式 + 公式"查找已经存在的节点组；没有则返回 None。"""
    sources = _normalize_sources(mode, sources)
    want = json.dumps(sources, ensure_ascii=False)
    for group in bpy.data.node_groups:
        if getattr(group, "bl_idname", "") != "GeometryNodeTree":
            continue
        try:
            if (
                group.get(_GROUP_PROP_MARKER)
                and group.get(_GROUP_PROP_MODE) == mode
                and group.get(_GROUP_PROP_FORMULA) == want
                and group.get(_GROUP_PROP_VERSION) == _GN_VERSION
            ):
                return group
        except (TypeError, AttributeError):
            continue
    return None


def get_or_create_formula_group(mode: str, sources) -> bpy.types.NodeTree:
    """查找可复用的节点组，没有就新建。"""
    sources = _normalize_sources(mode, sources)
    group = find_formula_group(mode, sources)
    if group is not None:
        return group
    return build_formula_group(mode, sources)


# ---------------------------------------------------------------------------
# 函数场节点（v1.3）：把公式变成可在任意几何节点树里使用的字段
# ---------------------------------------------------------------------------

def _field_sources(kind: str, settings: FunctionGenSettings) -> List[str]:
    """函数场用的公式：值场取高度公式，向量场取参数模式的 3 条公式。"""
    if kind == "SCALAR":
        return [settings.formula]
    if kind == "CURVE_VECTOR":
        return [settings.curve_x, settings.curve_y, settings.curve_z]
    if kind == "SURFACE_VECTOR":
        return [settings.surf_x, settings.surf_y, settings.surf_z]
    raise GenerationError(f"未知的函数场类型：{kind}")


def _field_group_name(kind: str, sources: Sequence[str]) -> str:
    first = _short_formula(sources[0], 20)
    suffix = " +…" if len(sources) > 1 else ""
    label = {"SCALAR": "值场", "CURVE_VECTOR": "曲线场", "SURFACE_VECTOR": "曲面场"}.get(kind, "场")
    return f"函数公式·{label} [{first}{suffix}]"


def _create_field_interface(group, kind: str) -> None:
    specs = []
    if kind == "SCALAR":
        specs.append((_SOCKET_POSITION_IN, "NodeSocketVector"))
        output = (_SOCKET_FIELD_VALUE, "NodeSocketFloat")
    elif kind == "CURVE_VECTOR":
        specs.append((_SOCKET_T, "NodeSocketFloat"))
        output = (_SOCKET_POSITION_IN, "NodeSocketVector")
    else:
        specs.append((_SOCKET_U, "NodeSocketFloat"))
        specs.append((_SOCKET_V, "NodeSocketFloat"))
        output = (_SOCKET_POSITION_IN, "NodeSocketVector")
    for name in _SOCKET_PARAMS:
        specs.append((name, "NodeSocketFloat"))
    for name, socket_type in specs:
        item = group.interface.new_socket(name=name, in_out="INPUT", socket_type=socket_type)
        if socket_type == "NodeSocketFloat":
            try:
                item.default_value = 1.0 if name in _SOCKET_PARAMS else 0.0
            except (AttributeError, TypeError):
                pass
        elif socket_type == "NodeSocketVector":
            try:
                item.default_value = (0.0, 0.0, 0.0)
            except (AttributeError, TypeError):
                pass
    group.interface.new_socket(name=output[0], in_out="OUTPUT", socket_type=output[1])


def build_field_group(kind: str, sources, group=None) -> bpy.types.NodeTree:
    """创建/重建一个函数场节点组（公式烘焙，输入是参数/位置，输出是值或向量）。"""
    if kind not in _FIELD_KINDS:
        raise GenerationError(f"未知的函数场类型：{kind}")
    sources = [str(item) for item in sources]
    defaults = {name: 1.0 for name in PARAMETERS}
    if kind == "SCALAR":
        formulas = [
            Formula(
                sources[0], ("x", "y"),
                parameters=PARAMETERS, parameter_defaults=defaults,
            )
        ]
    elif kind == "CURVE_VECTOR":
        formulas = [
            Formula(
                source, PARAM_CURVE_VARIABLES,
                parameters=PARAMETERS, parameter_defaults=defaults,
            )
            for source in sources
        ]
    else:
        formulas = [
            Formula(
                source, PARAM_SURFACE_VARIABLES,
                parameters=PARAMETERS, parameter_defaults=defaults,
            )
            for source in sources
        ]

    if group is None:
        group = bpy.data.node_groups.new(_field_group_name(kind, sources), "GeometryNodeTree")
    else:
        try:
            group.name = _field_group_name(kind, sources)
        except Exception:
            pass
    group[_GROUP_PROP_MARKER] = True
    group[_GROUP_PROP_FIELD] = True
    group[_GROUP_PROP_FIELD_KIND] = kind
    group[_GROUP_PROP_VERSION] = _GN_VERSION
    group[_GROUP_PROP_FORMULA] = json.dumps(sources, ensure_ascii=False)
    group.description = (
        f"函数公式场：{' / '.join(sources)}\n"
        "几何节点里使用：把 Position 节点接到「位置」输入，输出就是公式值；"
        "参数 a-d 可以连接噪声/场景时间/编号等字段，让系数随空间或时间变化"
    )
    if len(group.interface.items_tree) == 0:
        _create_field_interface(group, kind)
    group.nodes.clear()
    input_node = _new_node(group, "NodeGroupInput", (-700.0, 0.0), label="输入")
    output_node = _new_node(group, "NodeGroupOutput", (1200.0, 0.0), label="输出")
    param_sockets = {
        var_name: input_node.outputs[socket_name]
        for var_name, socket_name in zip(PARAMETERS, _SOCKET_PARAMS)
    }

    if kind == "SCALAR":
        separate = _new_node(group, "ShaderNodeSeparateXYZ", (-400.0, 0.0), label="拆开位置")
        _link(group, input_node.outputs[_SOCKET_POSITION_IN], separate.inputs[0])
        compiler = _GNCompiler(
            group,
            {"x": separate.outputs[0], "y": separate.outputs[1], **param_sockets},
        )
        value = compiler._as_socket(compiler.compile(formulas[0].tree))
        _link(group, value, output_node.inputs[0])
    elif kind == "CURVE_VECTOR":
        compiler = _GNCompiler(
            group,
            {"t": input_node.outputs[_SOCKET_T], **param_sockets},
        )
        combine = _new_node(group, "ShaderNodeCombineXYZ", (800.0, 0.0), label="组合位置")
        for index, formula in enumerate(formulas):
            _link(group, compiler._as_socket(compiler.compile(formula.tree)), combine.inputs[index])
        _link(group, combine.outputs[0], output_node.inputs[0])
    else:
        compiler = _GNCompiler(
            group,
            {"u": input_node.outputs[_SOCKET_U], "v": input_node.outputs[_SOCKET_V], **param_sockets},
        )
        combine = _new_node(group, "ShaderNodeCombineXYZ", (800.0, 0.0), label="组合位置")
        for index, formula in enumerate(formulas):
            _link(group, compiler._as_socket(compiler.compile(formula.tree)), combine.inputs[index])
        _link(group, combine.outputs[0], output_node.inputs[0])
    return group


def get_or_create_field_group(kind: str, sources) -> bpy.types.NodeTree:
    sources = [str(item) for item in sources]
    want = json.dumps(sources, ensure_ascii=False)
    for group in bpy.data.node_groups:
        if not group.get(_GROUP_PROP_FIELD):
            continue
        if (
            group.get(_GROUP_PROP_FIELD_KIND) == kind
            and group.get(_GROUP_PROP_FORMULA) == want
            and group.get(_GROUP_PROP_VERSION) == _GN_VERSION
        ):
            return group
    return build_field_group(kind, sources)


def insert_field_node(context, kind: str, settings: FunctionGenSettings, tree=None):
    """把函数场节点组插入当前几何节点树（返回节点组）。"""
    sources = _field_sources(kind, settings)
    group = get_or_create_field_group(kind, sources)
    insert_group_node(context, group, tree=tree, auto_connect=False)
    return group


def update_field_nodes(context, settings: FunctionGenSettings) -> int:
    """用当前面板公式重建当前节点树里所有函数场节点。"""
    space = getattr(context, "space_data", None)
    if space is None or getattr(space, "type", "") != "NODE_EDITOR":
        return 0
    tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    if tree is None:
        return 0
    count = 0
    for node in list(tree.nodes):
        if getattr(node, "bl_idname", "") != "GeometryNodeGroup":
            continue
        group = getattr(node, "node_tree", None)
        if group is None or not group.get(_GROUP_PROP_FIELD):
            continue
        kind = group.get(_GROUP_PROP_FIELD_KIND)
        if kind not in _FIELD_KINDS:
            continue
        sources = _field_sources(kind, settings)
        build_field_group(kind, sources, group=group)
        node.label = group.name
        count += 1
    return count


# ---------------------------------------------------------------------------
# 把节点组用到物体 / 插入到当前节点树
# ---------------------------------------------------------------------------

def _set_modifier_input(modifier, identifier: str, value) -> bool:
    """给 NODES 修改器的输入 socket 赋值，兼容新旧 API。

    * Blender 5.x：输入值挂在 modifier.properties.inputs.<identifier>.value 上
      （从 5.x 起修改器不再支持 mod["Socket_x"] 这种 IDProperty 写法）；
    * Blender 4.x：直接 modifier["Socket_x"] = value。
    """
    try:
        inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
        if inputs is not None:
            socket_wrapper = getattr(inputs, identifier, None)
            if socket_wrapper is not None:
                socket_wrapper.value = value
                return True
    except Exception:
        pass
    try:  # 兼容 Blender 4.x
        modifier[identifier] = value
        return True
    except Exception:
        return False


def _set_modifier_inputs(modifier, group, settings: FunctionGenSettings) -> None:
    """把面板参数写入 NODES 修改器的输入 socket（通过接口 identifier 定位）。"""
    values = {
        _SOCKET_START: float(settings.range_start),
        _SOCKET_END: float(settings.range_end),
        _SOCKET_RANGE2_START: float(settings.range2_start),
        _SOCKET_RANGE2_END: float(settings.range2_end),
        _SOCKET_RESOLUTION: max(1, int(settings.resolution)),
        _SOCKET_RADIUS: float(settings.tube_radius),
        _SOCKET_CIRCLE_RES: max(3, int(settings.bevel_resolution) or 16),
        _SOCKET_FILL_CAPS: bool(settings.fill_caps),
        _SOCKET_ADAPTIVE: bool(settings.adaptive_sampling),
        _SOCKET_SMOOTH: bool(settings.smooth_shading),
        _SOCKET_ATTRS: bool(settings.gn_output_attributes),
        _SOCKET_SCALE: tuple(float(v) for v in settings.gn_scale),
        _SOCKET_OFFSET: tuple(float(v) for v in settings.gn_offset),
        _SOCKET_WELD: bool(settings.weld_seams),
        _SOCKET_PARAMS[0]: float(settings.param_a),
        _SOCKET_PARAMS[1]: float(settings.param_b),
        _SOCKET_PARAMS[2]: float(settings.param_c),
        _SOCKET_PARAMS[3]: float(settings.param_d),
    }
    material = getattr(settings, "material_target", None)
    if material is not None:
        values[_SOCKET_MATERIAL] = material
    for item in group.interface.items_tree:
        if getattr(item, "in_out", None) != "INPUT":
            continue
        name = getattr(item, "name", "")
        if name not in values:
            continue
        identifier = getattr(item, "identifier", "")
        # 改不进去不算致命错误（例如接口类型变了），由节点组默认值兜底
        _set_modifier_input(modifier, identifier, values[name])


def _material_socket_identifier(group):
    for item in group.interface.items_tree:
        if getattr(item, "in_out", None) == "INPUT" and item.name == _SOCKET_MATERIAL:
            return getattr(item, "identifier", "")
    return ""


def _set_modifier_material(modifier, group, material) -> None:
    """把材质写进修改器的「材质」输入（没有该接口时忽略）。"""
    if material is None:
        return
    identifier = _material_socket_identifier(group)
    if identifier:
        _set_modifier_input(modifier, identifier, material)


def _assign_object_material(obj, material) -> None:
    """把材质放到物体材质槽，方便用户在「材质属性/着色器」里直接找到并编辑。"""
    if material is None or obj.data is None:
        return
    try:
        slots = obj.data.materials
        if len(slots) == 0 or slots[0] != material:
            slots.clear()
            slots.append(material)
        obj.color = material.diffuse_color if hasattr(material, "diffuse_color") else (1, 1, 1, 1)
    except Exception:
        import traceback

        traceback.print_exc()


def _gn_object_sources(settings: FunctionGenSettings) -> List[tuple]:
    """返回几何节点要生成的所有物体：(公式名, 公式列表)。批量公式在高度场模式下生效。"""
    if settings.mode in HEIGHT_MODES:
        return [(source, [source]) for source in settings.height_formulas()]
    if settings.mode == "PARAM_CURVE":
        return [(settings.curve_x, [settings.curve_x, settings.curve_y, settings.curve_z])]
    if settings.mode == "PARAM_SURFACE":
        return [(settings.surf_x, [settings.surf_x, settings.surf_y, settings.surf_z])]
    raise GenerationError(f"未知的生成模式：{settings.mode}")


def _validate_ranges(settings: FunctionGenSettings) -> None:
    start = float(settings.range_start)
    end = float(settings.range_end)
    if not (math.isfinite(start) and math.isfinite(end)) or start >= end:
        raise GenerationError(f"范围起始值必须小于结束值（当前：{start:g} >= {end:g}）")
    if settings.mode == "PARAM_SURFACE":
        ustart = float(settings.range2_start)
        uend = float(settings.range2_end)
        if not (math.isfinite(ustart) and math.isfinite(uend)) or ustart >= uend:
            raise GenerationError(f"V 范围起始值必须小于结束值（当前：{ustart:g} >= {uend:g}）")


def _create_gn_single(
    context,
    settings: FunctionGenSettings,
    source_name: str,
    sources: Sequence[str],
) -> bpy.types.Object:
    group = get_or_create_formula_group(settings.mode, sources)
    label = _MODE_LABELS.get(settings.mode, settings.mode)
    short = _short_formula(source_name, 18)
    name = f"函数公式 {label}（几何节点） {short}".strip()
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)

    modifier = obj.modifiers.new("函数公式", "NODES")
    modifier.node_group = group
    _set_modifier_inputs(modifier, group, settings)
    # Python 直接改修改器输入后必须手动 tag，否则 depsgraph 可能继续用旧值
    # （Blender 5.x 实测；用户在 UI 里改参数时 Blender 会自动 tag）
    obj.update_tag()

    generators.save_settings_to_object(
        obj, settings, own_formula=source_name or sources[0]
    )
    generators.mark_gn_object(obj)
    obj.location = generators.cursor_location(context) if settings.at_cursor else (0.0, 0.0, 0.0)

    material = materials.ensure_object_material(obj, settings)
    if material is not None:
        _set_modifier_material(modifier, group, material)
        _assign_object_material(obj, material)
    generators.select_only(context, obj)
    return obj


def create_gn_objects(context, settings: FunctionGenSettings) -> List[bpy.types.Object]:
    """按当前参数生成几何节点物体（批量公式会生成多个），返回物体列表。"""
    _validate_ranges(settings)
    if settings.mode in PARAM_MODES:
        settings.param_formulas_or_raise()      # 提前校验 3 个公式，报错信息更具体
    objects = []
    for source_name, sources in _gn_object_sources(settings):
        objects.append(_create_gn_single(context, settings, source_name, sources))
    return objects


def create_gn_object(context, settings: FunctionGenSettings) -> bpy.types.Object:
    """兼容旧接口：生成并返回第一个物体。"""
    objects = create_gn_objects(context, settings)
    return objects[0] if objects else None


def insert_group_node(context, group: bpy.types.NodeTree, tree=None, auto_connect: bool = True) -> bpy.types.Node:
    """把节点组作为 Group 节点插入当前正在编辑的几何节点树（Add 菜单用）。

    auto_connect：为 True 且树的 Group Output 几何输入还空着时，顺手接上（几何体节点用）；
    函数场节点是 Float/Vector 输出，必须传 False，避免把标量接到几何插口上。
    """
    space = getattr(context, "space_data", None)
    if tree is None:
        if space is None or getattr(space, "type", "") != "NODE_EDITOR":
            raise GenerationError("请先打开几何节点编辑器")
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    if tree is None:
        raise GenerationError("当前没有正在编辑的节点树")
    if getattr(tree, "library", None) is not None:
        raise GenerationError("当前节点树是链接库数据，无法编辑")

    node = tree.nodes.new("GeometryNodeGroup")
    node.node_tree = group
    node.label = group.name
    # 放在鼠标/光标位置，多个节点时自动错开
    location = (-240.0, 0.0)
    if space is not None and hasattr(space, "cursor_location"):
        try:
            cursor = space.cursor_location
            location = (cursor[0], cursor[1])
        except (TypeError, AttributeError):
            pass
    node.location = location

    # 如果树里已经有 Group Output 且几何输入还空着，顺手连上，插入即可见效果
    output_node = next(
        (item for item in tree.nodes if getattr(item, "bl_idname", "") == "NodeGroupOutput"),
        None,
    )
    if auto_connect and output_node is not None and len(output_node.inputs) > 0:
        output_socket = output_node.inputs[0]
        if not output_socket.is_linked and node.outputs:
            _link(tree, node.outputs[0], output_socket)

    for other in tree.nodes:
        other.select = False
    node.select = True
    tree.nodes.active = node
    return node


def _update_settings_from_operator(settings: FunctionGenSettings, props) -> None:
    """把"插入节点"对话框里的输入写回场景参数，保持面板同步。"""
    settings.mode = props.mode
    settings.formula = props.formula
    settings.curve_x = props.curve_x
    settings.curve_y = props.curve_y
    settings.curve_z = props.curve_z
    settings.surf_x = props.surf_x
    settings.surf_y = props.surf_y
    settings.surf_z = props.surf_z
    settings.range_start = props.range_start
    settings.range_end = props.range_end
    settings.range2_start = props.range2_start
    settings.range2_end = props.range2_end
    settings.resolution = props.resolution
    settings.tube_radius = props.tube_radius


# ---------------------------------------------------------------------------
# 操作符
# ---------------------------------------------------------------------------

class FUNCTIONGEN_OT_create_gn_object(bpy.types.Operator):
    """根据当前面板参数生成带几何节点修改器的物体"""

    bl_idname = "functiongen.create_gn_object"
    bl_label = "生成几何节点对象"
    bl_description = "生成一个使用几何节点实现的函数物体（公式烘焙进节点，修改器里可调范围和分辨率）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        try:
            objects = create_gn_objects(context, settings)
        except (FormulaError, GenerationError) as exc:
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # 兜底
            import traceback

            traceback.print_exc()
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if len(objects) == 1:
            settings.set_status(f"已生成几何节点对象 {objects[0].name}")
        else:
            settings.set_status(f"已生成 {len(objects)} 个几何节点物体")
        return {"FINISHED"}


class FUNCTIONGEN_OT_add_gn_node(bpy.types.Operator):
    """在几何节点编辑器里插入一个"函数公式"节点（公式烘焙进节点）"""

    bl_idname = "functiongen.add_gn_node"
    bl_label = "函数公式节点"
    bl_description = "把当前公式编译成新的几何节点，插入到正在编辑的几何节点树里"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(name="生成模式", items=MODE_ITEMS, default="SURFACE")
    formula: StringProperty(name="公式", default="sin(x) * cos(y) * 2", maxlen=500)
    curve_x: StringProperty(name="x(t)", default="cos(t)", maxlen=500)
    curve_y: StringProperty(name="y(t)", default="sin(t)", maxlen=500)
    curve_z: StringProperty(name="z(t)", default="t / 3", maxlen=500)
    surf_x: StringProperty(name="x(u,v)", default="u", maxlen=500)
    surf_y: StringProperty(name="y(u,v)", default="v", maxlen=500)
    surf_z: StringProperty(name="z(u,v)", default="u * u - v * v", maxlen=500)
    range_start: FloatProperty(name="范围起始", default=-5.0, soft_min=-100.0, soft_max=100.0)
    range_end: FloatProperty(name="范围结束", default=5.0, soft_min=-100.0, soft_max=100.0)
    range2_start: FloatProperty(name="V 范围起始", default=-5.0, soft_min=-100.0, soft_max=100.0)
    range2_end: FloatProperty(name="V 范围结束", default=5.0, soft_min=-100.0, soft_max=100.0)
    resolution: IntProperty(name="分辨率", default=100, min=1, max=1000)
    tube_radius: FloatProperty(name="管道半径", default=0.1, min=0.0, soft_max=1.0)

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return bool(
            space
            and getattr(space, "type", "") == "NODE_EDITOR"
            and getattr(space, "tree_type", "") == "GeometryNodeTree"
        )

    def invoke(self, context, event):
        # 对话框默认值 = 面板里当前的参数，用户可以直接改
        settings = context.scene.function_gen
        self.mode = settings.mode
        self.formula = settings.formula
        self.curve_x = settings.curve_x
        self.curve_y = settings.curve_y
        self.curve_z = settings.curve_z
        self.surf_x = settings.surf_x
        self.surf_y = settings.surf_y
        self.surf_z = settings.surf_z
        self.range_start = settings.range_start
        self.range_end = settings.range_end
        self.range2_start = settings.range2_start
        self.range2_end = settings.range2_end
        self.resolution = settings.resolution
        self.tube_radius = settings.tube_radius
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode", expand=True)
        if self.mode in HEIGHT_MODES:
            layout.prop(self, "formula", text="")
        elif self.mode == "PARAM_CURVE":
            layout.prop(self, "curve_x")
            layout.prop(self, "curve_y")
            layout.prop(self, "curve_z")
        else:
            layout.prop(self, "surf_x")
            layout.prop(self, "surf_y")
            layout.prop(self, "surf_z")
        row = layout.row(align=True)
        row.prop(self, "range_start")
        row.prop(self, "range_end")
        if self.mode == "PARAM_SURFACE":
            row = layout.row(align=True)
            row.prop(self, "range2_start")
            row.prop(self, "range2_end")
        layout.prop(self, "resolution")
        if self.mode in TUBE_MODES:
            layout.prop(self, "tube_radius")

    def execute(self, context):
        settings = context.scene.function_gen
        if self.mode in HEIGHT_MODES:
            sources = [self.formula]
        elif self.mode == "PARAM_CURVE":
            sources = [self.curve_x, self.curve_y, self.curve_z]
        else:
            sources = [self.surf_x, self.surf_y, self.surf_z]
        try:
            group = build_formula_group(self.mode, sources)
            insert_group_node(context, group)
        except (FormulaError, GenerationError) as exc:
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # 兜底
            import traceback

            traceback.print_exc()
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _update_settings_from_operator(settings, self)
        settings.set_status(f"已插入节点：{group.name}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 函数场节点操作符（v1.3）
# ---------------------------------------------------------------------------

_FIELD_ENUM_ITEMS = (
    ("SCALAR", "函数值场（x,y→值）", "把面板主公式作为字段：输入位置，输出函数值"),
    ("CURVE_VECTOR", "曲线向量场（t→xyz）", "把参数曲线 x(t),y(t),z(t) 作为向量场"),
    ("SURFACE_VECTOR", "曲面向量场（u,v→xyz）", "把参数曲面 x(u,v),y(u,v),z(u,v) 作为向量场"),
)


class FUNCTIONGEN_OT_add_gn_field(bpy.types.Operator):
    """把函数公式作为"场"节点插入当前几何节点树"""

    bl_idname = "functiongen.add_gn_field"
    bl_label = "插入函数场节点"
    bl_description = (
        "用当前面板公式创建一个函数场节点（值场/曲线向量场/曲面向量场），"
        "在几何节点里可以连噪声、场景时间、编号等字段来驱动公式和系数"
    )
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(name="函数场类型", items=_FIELD_ENUM_ITEMS, default="SCALAR")

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return bool(
            space
            and getattr(space, "type", "") == "NODE_EDITOR"
            and getattr(space, "tree_type", "") == "GeometryNodeTree"
        )

    def execute(self, context):
        settings = context.scene.function_gen
        try:
            group = insert_field_node(context, self.kind, settings)
        except (FormulaError, GenerationError) as exc:
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            import traceback

            traceback.print_exc()
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.set_status(f"已插入函数场节点：{group.name}")
        return {"FINISHED"}


class FUNCTIONGEN_OT_update_field_nodes(bpy.types.Operator):
    """用当前面板公式重建节点树里的函数场节点"""

    bl_idname = "functiongen.update_field_nodes"
    bl_label = "更新函数场节点"
    bl_description = "把当前节点树里所有函数场节点用面板最新公式重建"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return bool(
            space
            and getattr(space, "type", "") == "NODE_EDITOR"
            and getattr(space, "tree_type", "") == "GeometryNodeTree"
        )

    def execute(self, context):
        settings = context.scene.function_gen
        try:
            count = update_field_nodes(context, settings)
        except (FormulaError, GenerationError) as exc:
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            import traceback

            traceback.print_exc()
            settings.set_status(str(exc), is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.set_status(f"已更新 {count} 个函数场节点")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Add 菜单集成
# ---------------------------------------------------------------------------

_menu_targets: List[type] = []


def _draw_add_menu(self, context) -> None:
    """在几何节点编辑器的 Add 菜单里加一项"函数公式节点"。"""
    space = getattr(context, "space_data", None)
    if space is None or getattr(space, "type", "") != "NODE_EDITOR":
        return
    if getattr(space, "tree_type", "") != "GeometryNodeTree":
        return
    layout = self.layout
    layout.separator()
    layout.operator("functiongen.add_gn_node", text="函数公式节点（几何体）", icon="NODETREE")
    column = layout.column(align=True)
    column.label(text="函数场节点")
    for kind, label, _description in _FIELD_ENUM_ITEMS:
        operator = column.operator("functiongen.add_gn_field", text=label)
        operator.kind = kind
    column.operator("functiongen.update_field_nodes", text="更新节点树里的函数场节点", icon="FILE_REFRESH")


_CLASSES = (
    FUNCTIONGEN_OT_create_gn_object,
    FUNCTIONGEN_OT_add_gn_node,
    FUNCTIONGEN_OT_add_gn_field,
    FUNCTIONGEN_OT_update_field_nodes,
)


def _register_menu() -> None:
    """优先挂到几何节点专用菜单；找不到时退回通用 Add 菜单。"""
    for type_name in ("NODE_MT_geometry_node_add_all", "NODE_MT_add"):
        menu_type = getattr(bpy.types, type_name, None)
        if menu_type is None:
            continue
        try:
            menu_type.append(_draw_add_menu)
            _menu_targets.append(menu_type)
            break
        except Exception:
            continue


def _unregister_menu() -> None:
    for menu_type in _menu_targets:
        try:
            menu_type.remove(_draw_add_menu)
        except Exception:
            pass
    _menu_targets.clear()


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    _register_menu()
    start_live_sync_timer()


def unregister() -> None:
    stop_live_sync_timer()
    _unregister_menu()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


# ---------------------------------------------------------------------------
# 几何节点参数同步（v1.2）
# ---------------------------------------------------------------------------

_live_sync_pending = False


def _settings_sources(settings: FunctionGenSettings) -> List[str]:
    """当前面板要生成的公式（高度场取主公式；参数模式取 3 条）。"""
    if settings.mode in HEIGHT_MODES:
        return [settings.formula]
    if settings.mode == "PARAM_CURVE":
        return [settings.curve_x, settings.curve_y, settings.curve_z]
    if settings.mode == "PARAM_SURFACE":
        return [settings.surf_x, settings.surf_y, settings.surf_z]
    raise GenerationError(f"未知的生成模式：{settings.mode}")


def _object_sources(obj) -> List[str]:
    mode = obj.get(generators.OBJ_PROP_MODE)
    if mode in HEIGHT_MODES:
        return [obj.get(generators.OBJ_PROP_FORMULA, "")]
    if mode == "PARAM_CURVE":
        return [
            obj.get(generators.OBJ_PROP_CURVE_X, ""),
            obj.get(generators.OBJ_PROP_CURVE_Y, ""),
            obj.get(generators.OBJ_PROP_CURVE_Z, ""),
        ]
    if mode == "PARAM_SURFACE":
        return [
            obj.get(generators.OBJ_PROP_SURF_X, ""),
            obj.get(generators.OBJ_PROP_SURF_Y, ""),
            obj.get(generators.OBJ_PROP_SURF_Z, ""),
        ]
    return []


def _modifier_input_values(obj) -> dict:
    """读取 NODES 修改器当前暴露的输入值（接口名 -> 值）。"""
    values = {}
    for modifier in obj.modifiers:
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        for item in modifier.node_group.interface.items_tree:
            if getattr(item, "in_out", None) != "INPUT":
                continue
            identifier = getattr(item, "identifier", "")
            try:
                values[item.name] = getattr(modifier.properties.inputs, identifier).value
            except Exception:
                continue
    return values


def _expected_inputs(settings: FunctionGenSettings) -> dict:
    """面板参数对应的节点组输入值（与 _set_modifier_inputs 保持同一套规则）。"""
    return {
        _SOCKET_START: float(settings.range_start),
        _SOCKET_END: float(settings.range_end),
        _SOCKET_RANGE2_START: float(settings.range2_start),
        _SOCKET_RANGE2_END: float(settings.range2_end),
        _SOCKET_RESOLUTION: max(1, int(settings.resolution)),
        _SOCKET_RADIUS: float(settings.tube_radius),
        _SOCKET_CIRCLE_RES: max(3, int(settings.bevel_resolution) or 16),
        _SOCKET_FILL_CAPS: bool(settings.fill_caps),
        _SOCKET_ADAPTIVE: bool(settings.adaptive_sampling),
        _SOCKET_SMOOTH: bool(settings.smooth_shading),
        _SOCKET_ATTRS: bool(settings.gn_output_attributes),
        _SOCKET_SCALE: tuple(float(v) for v in settings.gn_scale),
        _SOCKET_OFFSET: tuple(float(v) for v in settings.gn_offset),
        _SOCKET_WELD: bool(settings.weld_seams),
        _SOCKET_PARAMS[0]: float(settings.param_a),
        _SOCKET_PARAMS[1]: float(settings.param_b),
        _SOCKET_PARAMS[2]: float(settings.param_c),
        _SOCKET_PARAMS[3]: float(settings.param_d),
    }


def _values_equal(expected, actual) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    if isinstance(expected, (tuple, list)):
        try:
            return len(expected) == len(actual) and all(
                abs(float(a) - float(b)) <= 1e-9 for a, b in zip(expected, actual)
            )
        except (TypeError, ValueError):
            return False
    if isinstance(expected, float):
        try:
            return abs(float(actual) - expected) <= 1e-9
        except (TypeError, ValueError):
            return False
    try:
        return int(expected) == int(actual)
    except (TypeError, ValueError):
        return expected == actual


def object_needs_sync(obj, settings: FunctionGenSettings) -> bool:
    """判断选中的几何节点物体是否与当前面板参数不一致。"""
    if not generators.is_gn_object(obj):
        return False
    stored_mode = obj.get(generators.OBJ_PROP_MODE)
    if stored_mode != settings.mode:
        return True
    stored_sources = [str(item).strip() for item in _object_sources(obj)]
    current_sources = [str(item).strip() for item in _settings_sources(settings)]
    if stored_sources != current_sources:
        return True

    modifier = next((item for item in obj.modifiers if item.type == "NODES"), None)
    group = None if modifier is None else modifier.node_group
    if group is None or group.get(_GROUP_PROP_VERSION) != _GN_VERSION:
        return True

    actual = _modifier_input_values(obj)
    for name, value in _expected_inputs(settings).items():
        if name in actual:
            if not _values_equal(value, actual[name]):
                return True
        elif name in {item.name for item in group.interface.items_tree}:
            # 接口里有但读不到值：也当作需要重新同步
            return True

    # 材质由 Blender 材质系统在着色器里管理；颜色设置变化不再触发几何节点重建，
    # 只有上面这些几何参数/公式变化才需要同步。
    return False


def sync_gn_object(context, obj, settings: FunctionGenSettings, force: bool = False) -> bool:
    """把当前面板参数同步到几何节点物体（重建节点组 / 更新修改器输入 / 更新材质）。

    返回是否实际做了修改。公式或模式变化时会重建节点组（组内公式是烘焙的）；
    范围/分辨率/半径/材质变化时只改修改器输入与材质。
    """
    if not generators.is_gn_object(obj):
        raise GenerationError("请先选中本插件生成的几何节点物体")
    if not force and not object_needs_sync(obj, settings):
        return False

    _validate_ranges(settings)
    sources = _settings_sources(settings)
    mode = settings.mode

    modifier = next((item for item in obj.modifiers if item.type == "NODES"), None)
    if modifier is None:
        modifier = obj.modifiers.new("函数公式", "NODES")
    group = modifier.node_group

    same_mode = (
        group is not None
        and obj.get(generators.OBJ_PROP_MODE) == mode
        and group.get(_GROUP_PROP_MODE) == mode
        and group.get(_GROUP_PROP_VERSION) == _GN_VERSION
        and len(group.interface.items_tree) > 0
    )
    formulas_json = json.dumps(sources, ensure_ascii=False)
    if same_mode and group.get(_GROUP_PROP_FORMULA) != formulas_json:
        # 公式变了：在同一个节点组里重建（引用它的修改器会自动更新）
        build_formula_group(mode, sources, group=group)
    elif not same_mode:
        group = get_or_create_formula_group(mode, sources)
        modifier.node_group = group

    _set_modifier_inputs(modifier, group, settings)
    _ensure_synced_material(modifier, group, obj, settings)
    obj.update_tag()
    generators.save_settings_to_object(obj, settings, own_formula=sources[0])
    return True


def _ensure_synced_material(modifier, group, obj, settings) -> None:
    """同步时保证材质不丢：不覆盖用户在修改器里手动指定的材质。"""
    identifier = _material_socket_identifier(group)
    if not identifier:
        return
    current = None
    try:
        current = getattr(modifier.properties.inputs, identifier).value
    except Exception:
        current = None
    if current is not None:
        _assign_object_material(obj, current)
        return
    material = getattr(settings, "material_target", None)
    if material is None:
        existing = obj.data.materials[0] if obj.data and len(obj.data.materials) else None
        material = existing
    if material is None:
        material = materials.ensure_object_material(obj, settings)
    if material is not None:
        _set_modifier_material(modifier, group, material)
        _assign_object_material(obj, material)


def schedule_live_sync() -> None:
    """延迟 0.35 秒执行一次同步（避免拖动数值时每帧重建节点组）。"""
    global _live_sync_pending
    if _live_sync_pending:
        return
    _live_sync_pending = True
    try:
        bpy.app.timers.register(_run_live_sync, first_interval=0.35)
    except Exception:
        _live_sync_pending = False


def _run_live_sync():
    global _live_sync_pending
    _live_sync_pending = False
    try:
        context = bpy.context
        if context is None or context.scene is None:
            return None
        settings = context.scene.function_gen
        if not getattr(settings, "live_sync_gn", True):
            return None
        obj = context.active_object
        if obj is None or not generators.is_gn_object(obj):
            return None
        if object_needs_sync(obj, settings):
            sync_gn_object(context, obj, settings)
    except Exception:
        import traceback

        traceback.print_exc()
    return None




_live_sync_timer_running = False
_sync_in_progress = False


def _live_sync_timer():
    """常驻定时器（GUI 下每 0.5 秒检查一次）：面板参数改了但用户没点按钮时自动同步。"""
    global _sync_in_progress
    if _sync_in_progress:
        return 0.5
    try:
        context = bpy.context
        if context is None or getattr(context, "scene", None) is None:
            return 0.5
        settings = getattr(context.scene, "function_gen", None)
        if settings is None or not getattr(settings, "live_sync_gn", True):
            return 0.5
        obj = getattr(context, "active_object", None)
        if obj is None or not generators.is_gn_object(obj):
            return 0.5
        if object_needs_sync(obj, settings):
            _sync_in_progress = True
            try:
                sync_gn_object(context, obj, settings)
            finally:
                _sync_in_progress = False
    except Exception:
        import traceback

        traceback.print_exc()
    return 0.5


def start_live_sync_timer() -> None:
    global _live_sync_timer_running
    if _live_sync_timer_running:
        return
    try:
        bpy.app.timers.register(_live_sync_timer, first_interval=0.8, persistent=True)
        _live_sync_timer_running = True
    except Exception:
        _live_sync_timer_running = False


def stop_live_sync_timer() -> None:
    global _live_sync_timer_running
    if not _live_sync_timer_running:
        return
    try:
        bpy.app.timers.unregister(_live_sync_timer)
    except Exception:
        pass
    _live_sync_timer_running = False


def maybe_schedule_sync(context) -> None:
    """面板绘制时调用：选中的几何节点物体和面板参数不一致就安排同步。"""
    try:
        settings = context.scene.function_gen
        if not getattr(settings, "live_sync_gn", True):
            return
        obj = context.active_object
        if obj is None or not generators.is_gn_object(obj):
            return
        if object_needs_sync(obj, settings):
            schedule_live_sync()
    except Exception:
        pass
