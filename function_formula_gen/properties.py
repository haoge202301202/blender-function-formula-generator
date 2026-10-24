# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""参数定义模块：面板上所有可调参数都集中在这里。

v1.1 新增
=========
* 参数方程模式：PARAM_CURVE（x(t)/y(t)/z(t)）、PARAM_SURFACE（x(u,v)/y(u,v)/z(u,v)，
  曲面额外有 U/V 两组范围）；
* 批量公式列表：extra_formulas（曲线圆管/曲面网格模式下一次生成多个物体）；
* 自动材质：auto_material / color_source / palette；
* 渲染预览：preview_engine / preview_resolution / preview_samples；
* 函数模块插入的目标字段：formula_target / formula_extra_index（不保存进 .blend）。

维护提示
========
* 新增参数：在 FunctionGenSettings 里加 bpy.props 属性；
  需要参与"读取参数/更新物体"就在 generators.OBJECT_PROP_KEYS 里登记；
  需要显示就在 ui.py 补 layout.prop。
* 新增模式：MODE_ITEMS 加枚举项 + generators.MODE_BUILDERS / gn_nodes._BODY_BUILDERS
  注册生成函数 + ui.py 里按模式显示对应公式字段。
* 示例公式：直接在 PRESETS 里加 dict（键见 _PRESET_KEYS 注释）。
"""
from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

# ---------------------------------------------------------------------------
# 生成模式（identifier 与 v1.0 保持一致，旧物体/旧参数完全兼容）
# ---------------------------------------------------------------------------
MODE_ITEMS = (
    (
        "TUBE",
        "曲线圆管 (Tube)",
        "沿 Y 轴生成一条路径，Z 轴高度由公式决定，用曲线 Bevel 倒角生成光滑圆管",
    ),
    (
        "SURFACE",
        "曲面网格 (Surface)",
        "在 XY 平面生成网格，Z 轴高度由公式决定，适合可视化二元函数",
    ),
    (
        "PARAM_CURVE",
        "参数曲线 (Param Curve)",
        "输入 x(t)、y(t)、z(t) 三个公式，生成任意空间曲线圆管（螺旋线、结等）",
    ),
    (
        "PARAM_SURFACE",
        "参数曲面 (Param Surface)",
        "输入 x(u,v)、y(u,v)、z(u,v) 三个公式，生成球面、环面、莫比乌斯带等真三维曲面",
    ),
)

# 高度场模式（单公式 + 批量列表）与参数模式（固定 3 个公式）
HEIGHT_MODES = ("TUBE", "SURFACE")
PARAM_MODES = ("PARAM_CURVE", "PARAM_SURFACE")
TUBE_MODES = ("TUBE", "PARAM_CURVE")

# 材质 / 预览枚举
COLOR_SOURCE_ITEMS = (
    ("Z", "高度 Z", "按物体局部坐标的 Z 高度着色（最常用）"),
    ("RADIAL", "到中心距离", "按到物体中心的 XY 距离着色，适合球面/环面/螺旋线"),
    ("SOLID", "单色", "使用色带的中间颜色，不做渐变"),
)

PALETTE_ITEMS = (
    ("RAINBOW", "彩虹", "蓝 → 青 → 绿 → 黄 → 红"),
    ("WARM_COOL", "冷暖", "蓝 → 白 → 红"),
    ("MONO", "单色渐变", "深灰 → 亮白"),
    ("OCEAN", "海洋", "深蓝 → 青 → 浅黄"),
    ("CUSTOM", "自定义颜色", "使用下方自己新建/编辑的颜色色标（可增删、可调位置）"),
)

# 自定义颜色的默认色标（第一次选择「自定义颜色」时自动填充）
DEFAULT_CUSTOM_STOPS = (
    (0.0, (0.05, 0.15, 0.90, 1.0)),
    (0.5, (0.00, 0.85, 0.85, 1.0)),
    (1.0, (1.00, 0.78, 0.10, 1.0)),
)

PREVIEW_ENGINE_ITEMS = (
    ("FOLLOW", "跟随场景引擎", "使用当前场景的渲染引擎，不改变场景设置"),
    ("EEVEE", "EEVEE", "速度快，适合快速预览"),
    ("CYCLES", "Cycles (CPU)", "质量更高，速度较慢，CPU 渲染"),
)

PREVIEW_RESOLUTION_ITEMS = (
    ("640x480", "640 x 480", "快速预览"),
    ("960x720", "960 x 720", "默认预览尺寸"),
    ("1280x960", "1280 x 960", "高清预览"),
    ("1920x1080", "1920 x 1080 (16:9)", "横版高清"),
)

# 函数模块插入目标（对应面板上的哪个公式输入框）
FORMULA_TARGET_ITEMS = (
    ("MAIN", "主公式", ""),
    ("EXTRA", "附加公式", ""),
    ("CX", "参数曲线 x(t)", ""),
    ("CY", "参数曲线 y(t)", ""),
    ("CZ", "参数曲线 z(t)", ""),
    ("SX", "参数曲面 x(u,v)", ""),
    ("SY", "参数曲面 y(u,v)", ""),
    ("SZ", "参数曲面 z(u,v)", ""),
)


# ---------------------------------------------------------------------------
# 批量公式列表：每一项就是一条 formula
# ---------------------------------------------------------------------------
class FunctionFormulaItem(PropertyGroup):
    formula: StringProperty(
        name="公式",
        description="关于 x / y 的数学表达式（和主公式使用同一个范围与分辨率）",
        default="sin(x) * 2",
        maxlen=500,
    )


class FunctionColorStop(PropertyGroup):
    """自定义渐变色标：颜色 + 位置（0~1，位置小的在前）。"""

    color: FloatVectorProperty(
        name="颜色",
        description="这个色标的颜色（鼠标点开取色器，可自己新建任意颜色）",
        subtype="COLOR",
        size=4,
        default=(0.2, 0.6, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )
    position: FloatProperty(
        name="位置",
        description="在渐变里的位置：0=起点，1=终点",
        default=0.5,
        min=0.0,
        max=1.0,
        soft_min=0.0,
        soft_max=1.0,
    )


def _on_palette_changed(settings, context) -> None:
    """选择「自定义颜色」且列表为空时，自动填一套默认色标，用户可直接改。"""
    try:
        if settings.palette == "CUSTOM" and len(settings.custom_colors) == 0:
            settings.fill_custom_defaults()
    except Exception:
        pass


class FunctionGenSettings(PropertyGroup):
    """场景级参数组：3D 视图侧栏面板与几何节点编辑器面板共用。"""

    # --- 核心参数（需求文档要求） -----------------------------------------
    mode: EnumProperty(
        name="生成模式",
        description="选择生成哪种几何体",
        items=MODE_ITEMS,
        default="TUBE",
    )
    formula: StringProperty(
        name="公式",
        description=(
            "关于 x / y 的数学表达式，例如 sin(x) * 2、cos(y)、x*x + y*y。"
            "支持 sin/cos/tan/sqrt/exp/log/abs/floor... 等函数，pi、e 等常量；"
            "也可以点击旁边的 ƒ 按钮插入函数模块"
        ),
        default="sin(x) * 2",
        maxlen=500,
    )
    extra_formulas: CollectionProperty(
        type=FunctionFormulaItem,
        name="附加公式",
        description="例如同时生成 sin(x)、cos(x)、sin(x)*cos(x) 三条曲线",
    )
    extra_formulas_index: IntProperty(
        name="当前附加公式",
        default=0,
        min=0,
        options={"SKIP_SAVE"},
    )
    range_start: FloatProperty(
        name="范围起始",
        description="采样范围的起始值（曲面模式下 X、Y 都用这个范围；参数模式是参数 t / u 的起始）",
        default=-5.0,
        soft_min=-100.0,
        soft_max=100.0,
        step=10,
        precision=4,
    )
    range_end: FloatProperty(
        name="范围结束",
        description="采样范围的结束值（参数模式是参数 t / u 的结束）",
        default=5.0,
        soft_min=-100.0,
        soft_max=100.0,
        step=10,
        precision=4,
    )
    range2_start: FloatProperty(
        name="第二范围起始",
        description="参数曲面的 V 参数起始值（球面/环面需要单独设置）",
        default=-5.0,
        soft_min=-100.0,
        soft_max=100.0,
        step=10,
        precision=4,
    )
    range2_end: FloatProperty(
        name="第二范围结束",
        description="参数曲面的 V 参数结束值",
        default=5.0,
        soft_min=-100.0,
        soft_max=100.0,
        step=10,
        precision=4,
    )
    resolution: IntProperty(
        name="分辨率",
        description="分段数：圆管 = 路径分段数；曲面 = 每边分段数（顶点数 = (分辨率+1)²）",
        default=100,
        min=1,
        max=1000,
        soft_min=2,
        soft_max=300,
    )
    adaptive_sampling: BoolProperty(
        name="自动加密采样",
        description=(
            "根据范围自动增加采样点：范围越大采样越密，避免大范围时曲线抖动/形状不准。"
            "小范围时保持你设置的分段数不变（推荐开启）"
        ),
        default=True,
    )
    tube_radius: FloatProperty(
        name="管道半径",
        description="圆管半径，仅在圆管类模式下生效",
        default=0.1,
        min=0.0,
        soft_max=1.0,
        precision=4,
        step=1,
    )

    # --- 参数方程公式（v1.1） ---------------------------------------------
    curve_x: StringProperty(
        name="x(t)",
        description="参数曲线的 X 坐标公式，变量 t",
        default="cos(t)",
        maxlen=500,
    )
    curve_y: StringProperty(
        name="y(t)",
        description="参数曲线的 Y 坐标公式，变量 t",
        default="sin(t)",
        maxlen=500,
    )
    curve_z: StringProperty(
        name="z(t)",
        description="参数曲线的 Z 坐标公式，变量 t",
        default="t / 3",
        maxlen=500,
    )
    surf_x: StringProperty(
        name="x(u,v)",
        description="参数曲面的 X 坐标公式，变量 u / v",
        default="u",
        maxlen=500,
    )
    surf_y: StringProperty(
        name="y(u,v)",
        description="参数曲面的 Y 坐标公式，变量 u / v",
        default="v",
        maxlen=500,
    )
    surf_z: StringProperty(
        name="z(u,v)",
        description="参数曲面的 Z 坐标公式，变量 u / v",
        default="u * u - v * v",
        maxlen=500,
    )
    weld_seams: BoolProperty(
        name="焊接接缝",
        description="参数曲面首尾重合的顶点自动焊起来（球面/环面需要；关闭则保留接缝）",
        default=True,
    )

    # --- 自动材质（v1.1） -------------------------------------------------
    auto_material: BoolProperty(
        name="自动创建材质",
        description=(
            "生成时如果物体还没有材质，就自动创建一份 Blender 材质并放进材质槽；"
            "材质完全可以在 Shader Editor 里改色/改节点"
        ),
        default=True,
    )
    material_target: PointerProperty(
        name="材质",
        description=(
            "统一使用的 Blender 材质：设置后新物体都用它（几何节点会写进节点组的「材质」输入）；"
            "留空则自动创建/沿用物体已有材质"
        ),
        type=bpy.types.Material,
    )
    color_source: EnumProperty(
        name="着色依据",
        description="渐变色按什么值计算",
        items=COLOR_SOURCE_ITEMS,
        default="Z",
    )
    palette: EnumProperty(
        name="色带",
        description="渐变色使用哪套颜色；选「自定义颜色」后可在下面增删自己的颜色",
        items=PALETTE_ITEMS,
        default="RAINBOW",
        update=_on_palette_changed,
    )
    solid_color: FloatVectorProperty(
        name="单色",
        description="着色依据为「单色」时使用的颜色；可自己新建任意颜色",
        subtype="COLOR",
        size=4,
        default=(0.10, 0.55, 1.00, 1.0),
        min=0.0,
        max=1.0,
    )
    custom_colors: CollectionProperty(
        name="自定义颜色",
        description="自定义渐变的色标列表（位置 0~1，自动按位置排序）",
        type=FunctionColorStop,
    )
    custom_colors_index: IntProperty(
        name="当前自定义颜色",
        default=0,
        min=0,
    )

    # --- 一键渲染预览（v1.1） ---------------------------------------------
    preview_engine: EnumProperty(
        name="预览引擎",
        description="渲染预览使用的引擎；「跟随场景」不会改动当前场景设置",
        items=PREVIEW_ENGINE_ITEMS,
        default="FOLLOW",
    )
    preview_resolution: EnumProperty(
        name="预览尺寸",
        description="渲染预览的图片分辨率",
        items=PREVIEW_RESOLUTION_ITEMS,
        default="960x720",
    )
    preview_samples: IntProperty(
        name="预览采样",
        description="预览渲染的采样数，越小越快",
        default=32,
        min=1,
        max=4096,
        soft_max=256,
    )
    preview_last_path: StringProperty(
        name="上次预览图路径",
        description="最近一次渲染预览保存的图片路径（用于「打开预览图」按钮）",
        default="",
        options={"SKIP_SAVE"},
    )

    # --- 奇异点处理（v1.1：1/x 在 x=0 这类无定义点） -----------------------
    singular_handling: EnumProperty(
        name="无定义点处理",
        description=(
            "公式在个别采样点无定义时怎么办（例如 1/x 在 x=0、log(0)、tan(90°)）。"
            "自动容错：曲线在无定义处断开成多段，曲面用邻近值填充，保证能生成；"
            "严格报错：立即停止并提示出错的坐标"
        ),
        items=(
            ("AUTO", "自动容错（推荐）", "无定义点自动跳过/填充，曲线会断开成多段，曲面用邻近值补"),
            ("STRICT", "严格报错", "遇到任何无定义点立即报错，并提示出错坐标"),
        ),
        default="AUTO",
    )

    # --- 高级参数（默认值即可直接使用） -----------------------------------
    convert_to_mesh: BoolProperty(
        name="圆管转换为网格",
        description="把带倒角的曲线转换成网格物体（方便继续编辑/加修改器）；关闭则保留曲线",
        default=True,
    )
    smooth_shading: BoolProperty(
        name="平滑着色",
        description="生成后自动开启 Shade Smooth",
        default=True,
    )
    fill_caps: BoolProperty(
        name="圆管封口",
        description="圆管两端是否封口（几何节点版本为节点输入「封口」，可单独调）",
        default=True,
    )

    # --- 公式系数参数（v1.3） ---------------------------------------------
    param_a: FloatProperty(
        name="参数 a",
        description="公式里的系数 a（默认 1）；几何节点版本会作为节点输入，可连噪声/场景时间/编号等字段来变化",
        default=1.0,
    )
    param_b: FloatProperty(
        name="参数 b",
        description="公式里的系数 b（默认 1）",
        default=1.0,
    )
    param_c: FloatProperty(
        name="参数 c",
        description="公式里的系数 c（默认 1）",
        default=1.0,
    )
    param_d: FloatProperty(
        name="参数 d",
        description="公式里的系数 d（默认 1）",
        default=1.0,
    )

    # --- 几何节点专用输出选项（v1.3） -------------------------------------
    gn_output_attributes: BoolProperty(
        name="输出属性",
        description=(
            "在几何节点物体上写入「函数值」/「参数」命名字段，方便下游几何节点或材质读取；"
            "对应节点组输入「输出属性」"
        ),
        default=True,
    )
    gn_scale: FloatVectorProperty(
        name="坐标缩放",
        description="生成后对最终几何体整体缩放（对应节点组输入「坐标缩放」）",
        size=3,
        default=(1.0, 1.0, 1.0),
        subtype="XYZ",
    )
    gn_offset: FloatVectorProperty(
        name="位置偏移",
        description="生成后对最终几何体整体偏移（对应节点组输入「位置偏移」）",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype="XYZ",
    )
    at_cursor: BoolProperty(
        name="放置在 3D 光标处",
        description="把生成的物体放在 3D 光标位置；关闭则放在世界原点",
        default=True,
    )
    curve_resolution: IntProperty(
        name="曲线细分",
        description="Bezier 曲线每一段的细分数，越大圆管路径越顺滑",
        default=12,
        min=1,
        max=64,
        soft_max=32,
    )
    bevel_resolution: IntProperty(
        name="圆管截面细分",
        description="圆管横截面的细分，越大截面越圆（0 为最粗糙的方形截面）",
        default=10,
        min=0,
        max=32,
        soft_max=16,
    )

    # --- 函数模块插入目标（不保存进文件） ---------------------------------
    formula_target: EnumProperty(
        name="插入目标",
        description="函数模块按钮把内容追加到哪个公式输入框",
        items=FORMULA_TARGET_ITEMS,
        default="MAIN",
        options={"SKIP_SAVE"},
    )
    formula_extra_index: IntProperty(
        name="附加公式序号",
        description="函数模块插入到第几条附加公式",
        default=0,
        min=0,
        options={"SKIP_SAVE"},
    )

    # --- 数学键盘（Desmos 风格输入）状态，不保存进文件 ---------------------
    kbd_active: BoolProperty(
        name="数学键盘开启",
        default=False,
        options={"SKIP_SAVE"},
    )
    kbd_target: EnumProperty(
        name="键盘编辑目标",
        description="数学键盘正在编辑哪个公式输入框",
        items=FORMULA_TARGET_ITEMS,
        default="MAIN",
        options={"SKIP_SAVE"},
    )
    kbd_extra_index: IntProperty(
        name="键盘编辑的附加公式序号",
        default=0,
        min=0,
        options={"SKIP_SAVE"},
    )
    kbd_pos: IntProperty(
        name="光标位置",
        description="数学键盘在公式文本中的插入位置",
        default=0,
        min=0,
        options={"SKIP_SAVE"},
    )
    kbd_last_text: StringProperty(
        name="键盘文本缓存",
        default="",
        options={"SKIP_SAVE"},
    )

    # --- 几何节点实时同步（v1.2） -----------------------------------------
    live_sync_gn: BoolProperty(
        name="几何节点参数实时同步",
        description=(
            "在面板里修改公式或参数时，自动同步到当前选中的几何节点物体"
            "（公式/模式变化会重建节点组，范围/分辨率/半径会直接改修改器输入）"
        ),
        default=True,
    )

    # --- 状态提示（只影响面板显示，不保存进 .blend 文件） ------------------
    last_message: StringProperty(
        name="状态",
        description="最近一次操作的结果提示",
        default="",
        options={"SKIP_SAVE"},
    )
    last_error: BoolProperty(
        name="上次操作失败",
        default=False,
        options={"SKIP_SAVE"},
    )

    # ------------------------------------------------------------------
    # 便捷方法（各模块共用，避免到处写 mode 判断）
    # ------------------------------------------------------------------
    # -- 自定义渐变颜色 ----------------------------------------------------
    def fill_custom_defaults(self) -> None:
        """用默认色标填充自定义颜色列表（第一次选「自定义颜色」时调用）。"""
        self.custom_colors.clear()
        for position, color in DEFAULT_CUSTOM_STOPS:
            item = self.custom_colors.add()
            item.position = position
            item.color = color
        self.custom_colors_index = min(
            max(0, int(self.custom_colors_index)),
            max(0, len(self.custom_colors) - 1),
        )

    def custom_palette_stops(self) -> list:
        """返回按位置排序的自定义色标 [(position, (r,g,b)), ...]；空列表用默认色标。"""
        stops = []
        for item in self.custom_colors:
            try:
                position = min(1.0, max(0.0, float(item.position)))
                color = item.color
                stops.append((position, (float(color[0]), float(color[1]), float(color[2]))))
            except (TypeError, ValueError, IndexError):
                continue
        if not stops:
            return [(position, color[:3]) for position, color in DEFAULT_CUSTOM_STOPS]
        stops.sort(key=lambda pair: pair[0])
        if len(stops) == 1:
            position, color = stops[0]
            stops = [(0.0, color), (1.0, color)]
        return stops

    def parameter_values(self) -> dict:
        """公式系数参数 a/b/c/d 的当前值。"""
        return {
            "a": float(self.param_a),
            "b": float(self.param_b),
            "c": float(self.param_c),
            "d": float(self.param_d),
        }

    def formula_or_raise(self, source=None):
        """返回经过安全校验的 formula.Formula 对象；失败抛 FormulaError。"""
        from .formula import PARAMETERS, Formula

        mode = self.mode
        if mode == "PARAM_CURVE":
            raise ValueError("参数曲线有 3 个公式，请用 param_formulas_or_raise()")
        if mode == "PARAM_SURFACE":
            raise ValueError("参数曲面有 3 个公式，请用 param_formulas_or_raise()")
        return Formula(
            self.formula if source is None else source,
            parameters=PARAMETERS,
            parameter_defaults=self.parameter_values(),
        )

    def param_formulas_or_raise(self):
        """参数模式：返回 (Formula, Formula, Formula) 三元组。"""
        from .formula import (
            PARAM_CURVE_VARIABLES,
            PARAM_SURFACE_VARIABLES,
            PARAMETERS,
            Formula,
        )

        if self.mode == "PARAM_CURVE":
            variables = PARAM_CURVE_VARIABLES
            sources = (self.curve_x, self.curve_y, self.curve_z)
            labels = ("x(t)", "y(t)", "z(t)")
        elif self.mode == "PARAM_SURFACE":
            variables = PARAM_SURFACE_VARIABLES
            sources = (self.surf_x, self.surf_y, self.surf_z)
            labels = ("x(u,v)", "y(u,v)", "z(u,v)")
        else:
            raise ValueError("当前模式不是参数方程模式")

        formulas = []
        defaults = self.parameter_values()
        for label, source in zip(labels, sources):
            try:
                formulas.append(
                    Formula(
                        source,
                        variables,
                        parameters=PARAMETERS,
                        parameter_defaults=defaults,
                    )
                )
            except Exception as exc:  # FormulaError：补上字段名，方便定位是哪个公式错
                raise type(exc)(f"{label} 公式错误：{exc}") from None
        return tuple(formulas)

    def height_formulas(self):
        """高度场模式：返回所有要生成的公式源码（主公式 + 启用的附加公式）。"""
        sources = [self.formula]
        for item in self.extra_formulas:
            text = item.formula.strip()
            if text:
                sources.append(item.formula)
        return sources

    def is_param_mode(self) -> bool:
        return self.mode in PARAM_MODES

    def is_tube_mode(self) -> bool:
        return self.mode in TUBE_MODES

    # -- 函数模块插入目标 --------------------------------------------------
    def _target_field(self, slot: str, extra_index: int = 0):
        """根据插入目标返回 (属性名, 集合序号或 None)。"""
        if slot == "MAIN":
            return "formula", None
        if slot == "EXTRA":
            if not self.extra_formulas:
                return "formula", None
            index = max(0, min(len(self.extra_formulas) - 1, extra_index))
            return "extra_formulas", index
        mapping = {
            "CX": "curve_x",
            "CY": "curve_y",
            "CZ": "curve_z",
            "SX": "surf_x",
            "SY": "surf_y",
            "SZ": "surf_z",
        }
        return mapping.get(slot, "formula"), None

    def get_target_text(self) -> str:
        """当前插入目标字段的文本。"""
        attr, index = self._target_field(self.formula_target, self.formula_extra_index)
        if index is None:
            return getattr(self, attr)
        return self.extra_formulas[index].formula

    def append_to_target(self, token: str) -> None:
        """把文本片段追加到当前插入目标（函数模块按钮用）。"""
        attr, index = self._target_field(self.formula_target, self.formula_extra_index)
        if index is None:
            setattr(self, attr, getattr(self, attr) + token)
        else:
            item = self.extra_formulas[index]
            item.formula = item.formula + token

    # -- 数学键盘（v1.2）：自己维护光标位置，支持在公式中间插入/删除 ----------
    def kbd_text(self) -> str:
        attr, index = self._target_field(self.kbd_target, self.kbd_extra_index)
        if index is None:
            return getattr(self, attr)
        return self.extra_formulas[index].formula

    def _kbd_write(self, text: str) -> None:
        attr, index = self._target_field(self.kbd_target, self.kbd_extra_index)
        if index is None:
            setattr(self, attr, text)
        else:
            self.extra_formulas[index].formula = text

    def _kbd_sync_pos(self) -> str:
        """如果用户在文本家里手动改过公式，光标默认回到末尾。"""
        text = self.kbd_text()
        if text != self.kbd_last_text:
            self.kbd_pos = len(text)
            self.kbd_last_text = text
        return text

    def kbd_open(self, target: str, extra_index: int = 0) -> None:
        self.kbd_target = target
        self.kbd_extra_index = max(0, int(extra_index))
        self.kbd_active = True
        text = self.kbd_text()
        self.kbd_pos = len(text)
        self.kbd_last_text = text

    def kbd_close(self) -> None:
        self.kbd_active = False

    def kbd_insert(self, token: str, back: int = 0, skip_closing: bool = False) -> None:
        """在光标处插入片段；back>0 时光标停在插入内容倒数第 back 个字符前。"""
        if not token:
            return
        text = self._kbd_sync_pos()
        pos = max(0, min(int(self.kbd_pos), len(text)))
        # 点 ")" 时如果光标右边已经是 ")"，直接跳过而不是重复补括号
        if token == ")" and pos < len(text) and text[pos] == ")":
            self.kbd_pos = pos + 1
            self.kbd_last_text = text
            return
        if skip_closing:
            while pos < len(text) and text[pos] == ")":
                pos += 1
        new_text = text[:pos] + token + text[pos:]
        new_pos = max(0, min(pos + len(token) - max(0, int(back)), len(new_text)))
        self._kbd_write(new_text)
        self.kbd_pos = new_pos
        self.kbd_last_text = new_text

    def kbd_backspace(self) -> None:
        text = self._kbd_sync_pos()
        pos = max(0, min(int(self.kbd_pos), len(text)))
        if pos <= 0:
            return
        removed = text[pos - 1]
        new_text = text[: pos - 1] + text[pos:]
        new_pos = pos - 1
        # 删除 "(" 时如果紧跟 ")"，把一对括号一起删掉（sin() -> 空）
        if removed == "(" and new_pos < len(new_text) and new_text[new_pos] == ")":
            new_text = new_text[:new_pos] + new_text[new_pos + 1:]
        self._kbd_write(new_text)
        self.kbd_pos = new_pos
        self.kbd_last_text = new_text

    def kbd_delete(self) -> None:
        text = self._kbd_sync_pos()
        pos = max(0, min(int(self.kbd_pos), len(text)))
        if pos >= len(text):
            return
        new_text = text[:pos] + text[pos + 1:]
        self._kbd_write(new_text)
        self.kbd_last_text = new_text

    def kbd_move(self, delta: int) -> None:
        text = self._kbd_sync_pos()
        self.kbd_pos = max(0, min(len(text), int(self.kbd_pos) + int(delta)))

    def kbd_home(self) -> None:
        self._kbd_sync_pos()
        self.kbd_pos = 0

    def kbd_end(self) -> None:
        text = self._kbd_sync_pos()
        self.kbd_pos = len(text)

    def kbd_clear(self) -> None:
        self._kbd_write("")
        self.kbd_pos = 0
        self.kbd_last_text = ""

    def reset_runtime_state(self) -> None:
        """把"运行时状态"恢复到默认值。

        Blender 的嵌套 PropertyGroup 属性即使标了 SKIP_SAVE 也可能被写进 .blend，
        所以这里在加载文件 / 注册插件时主动清空键盘、插入目标、状态提示等临时状态。
        """
        self.kbd_active = False
        self.kbd_target = "MAIN"
        self.kbd_extra_index = 0
        self.kbd_pos = 0
        self.kbd_last_text = ""
        self.formula_target = "MAIN"
        self.formula_extra_index = 0
        self.preview_last_path = ""
        self.last_message = ""
        self.last_error = False

    # -- 状态 --------------------------------------------------------------
    def set_status(self, message: str, is_error: bool = False) -> None:
        self.last_message = message
        self.last_error = is_error


# ---------------------------------------------------------------------------
# 示例公式（PRESETS）
# 每个 dict 支持的键：
#   name(必填), mode(默认 SURFACE), formula, extras(list), curve(tuple 3),
#   surf(tuple 3), start, end, range2(tuple), radius, resolution
# ---------------------------------------------------------------------------
def _preset(name, mode="SURFACE", **kwargs):
    data = {"name": name, "mode": mode}
    data.update(kwargs)
    return data


PRESETS = (
    _preset("正弦曲线圆管", "TUBE", formula="sin(x) * 2", start=-5.0, end=5.0, radius=0.1),
    _preset("余弦曲面", "SURFACE", formula="cos(y) * 2", start=-5.0, end=5.0),
    _preset("水波纹曲面", "SURFACE", formula="sin(x) * cos(y) * 2", start=-5.0, end=5.0),
    _preset(
        "墨西哥草帽",
        "SURFACE",
        formula="5 * sin(sqrt(x*x + y*y)) / (sqrt(x*x + y*y) + 0.001)",
        start=-8.0,
        end=8.0,
    ),
    _preset("抛物面", "SURFACE", formula="0.1 * (x*x + y*y)", start=-5.0, end=5.0),
    _preset("高斯钟形", "SURFACE", formula="3 * exp(-(x*x + y*y) / 4)", start=-6.0, end=6.0),
    _preset(
        "批量对比：sin / cos / sin*cos",
        "TUBE",
        formula="sin(x) * 2",
        extras=["cos(x) * 2", "sin(x) * cos(x) * 2"],
        start=-5.0,
        end=5.0,
        radius=0.1,
    ),
    _preset(
        "螺旋线（参数曲线）",
        "PARAM_CURVE",
        curve=("cos(t)", "sin(t)", "t / 3"),
        start=0.0,
        end=12.5663706,
        radius=0.12,
    ),
    _preset(
        "八字结（参数曲线）",
        "PARAM_CURVE",
        curve=("sin(t) + 2 * sin(2*t)", "cos(t) - 2 * cos(2*t)", "-sin(3*t)"),
        start=0.0,
        end=6.2831853,
        radius=0.15,
    ),
    _preset(
        "球面（参数曲面）",
        "PARAM_SURFACE",
        surf=("sin(u) * cos(v)", "sin(u) * sin(v)", "cos(u)"),
        start=0.0,
        end=3.1415927,
        range2=(0.0, 6.2831853),
        resolution=80,
    ),
    _preset(
        "环面（参数曲面）",
        "PARAM_SURFACE",
        surf=("(2 + cos(v)) * cos(u)", "(2 + cos(v)) * sin(u)", "sin(v)"),
        start=0.0,
        end=6.2831853,
        range2=(0.0, 6.2831853),
        resolution=80,
    ),
    _preset(
        "莫比乌斯带（参数曲面）",
        "PARAM_SURFACE",
        surf=(
            "(1 + v/2 * cos(u/2)) * cos(u)",
            "(1 + v/2 * cos(u/2)) * sin(u)",
            "v/2 * sin(u/2)",
        ),
        start=0.0,
        end=6.2831853,
        range2=(-1.0, 1.0),
        resolution=100,
    ),
    _preset(
        "双曲抛物面（参数曲面）",
        "PARAM_SURFACE",
        surf=("u", "v", "u * u - v * v"),
        start=-5.0,
        end=5.0,
        range2=(-5.0, 5.0),
        resolution=80,
    ),
)


# 注意：PropertyGroup 子类必须先用 bpy.utils.register_class 注册，
# 否则 PointerProperty(type=...) 会报 "may not be registered"。
_CLASSES = (FunctionFormulaItem, FunctionColorStop, FunctionGenSettings)


def _reset_all_scenes() -> None:
    """清空所有场景的运行时状态；注册/加载早期 bpy.data 可能是受限代理，必须容错。"""
    try:
        scenes = bpy.data.scenes
    except (AttributeError, ReferenceError):
        return
    try:
        scenes = list(scenes)
    except (AttributeError, TypeError, ReferenceError):
        return
    for scene in scenes:
        try:
            settings = getattr(scene, "function_gen", None)
        except (AttributeError, ReferenceError):
            continue
        if settings is not None:
            try:
                settings.reset_runtime_state()
            except Exception:
                pass


def _deferred_reset():
    """启动完成后再清一次运行时状态（注册阶段 bpy.data 可能受限）。"""
    try:
        _reset_all_scenes()
    except Exception:
        pass
    return None


@bpy.app.handlers.persistent
def _on_load_post(*_args) -> None:
    """打开 .blend 后清空键盘/插入目标/状态提示等运行时状态。"""
    try:
        _reset_all_scenes()
    except Exception:
        pass


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.function_gen = PointerProperty(
        type=FunctionGenSettings,
        name="函数公式生成器参数",
    )
    # 启用插件时（例如先打开旧文件再启用）也清一次运行时状态。
    # 注意：在插件注册阶段 bpy.data 可能是 _RestrictData（启动流程），
    # 因此这里必须容错，必要时延迟到事件循环启动后再清一次。
    try:
        _reset_all_scenes()
    except Exception:
        pass
    try:
        bpy.app.timers.register(_deferred_reset, first_interval=0.0)
    except Exception:
        pass
    try:
        bpy.app.handlers.load_post.append(_on_load_post)
    except Exception:
        pass


def unregister() -> None:
    try:
        bpy.app.handlers.load_post.remove(_on_load_post)
    except Exception:
        pass
    del bpy.types.Scene.function_gen
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
