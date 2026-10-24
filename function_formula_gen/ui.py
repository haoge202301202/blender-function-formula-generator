# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""界面模块：3D 视图侧栏面板 + 几何节点编辑器侧栏面板（v1.1）。

界面结构（3D 视图 > 侧栏 N > "函数生成"）
=========================================
* 生成模式（4 种：曲线圆管 / 曲面网格 / 参数曲线 / 参数曲面）
* 公式区：
    - 高度场模式：主公式 + 附加公式列表（批量生成多个物体）
    - 参数曲线：x(t) / y(t) / z(t)
    - 参数曲面：x(u,v) / y(u,v) / z(u,v) + U/V 两组范围
  每个公式框右边的 ƒ 按钮会打开"函数模块菜单"，点击函数/变量/运算符即可追加到该框。
* 采样范围（起始/结束，参数曲面有 V 范围）/ 分辨率 / 自动加密采样 / 管道半径
* 生成 / 校验公式 / 示例公式 / 读取参数 / 更新选中
* 外观：自动彩色材质（着色依据 + 色带）
* 预览：一键渲染（引擎 / 尺寸 / 采样）
* 高级选项 / 几何节点版本入口

维护提示
========
* 新增参数：properties.py 加属性，在这里用 layout.prop 显示（按模式放在对应位置）。
* 新增公式输入框：用 _draw_formula_row(layout, settings, prop, slot)，slot 需在
  properties.FORMULA_TARGET_ITEMS 里登记，并在 FUNCTIONGEN_MT_formula_tokens 里决定变量。
* 新增按钮：operators.py 加 Operator，然后这里 layout.operator("functiongen.xxx")。
* 面板只做显示与转发，不写业务逻辑。
"""
from __future__ import annotations

import os

import bpy

from . import generators, gn_nodes
from .formula import FormulaError
from .properties import (
    COLOR_SOURCE_ITEMS,
    HEIGHT_MODES,
    MODE_ITEMS,
    PALETTE_ITEMS,
    PARAM_MODES,
    PREVIEW_ENGINE_ITEMS,
    PREVIEW_RESOLUTION_ITEMS,
    PRESETS,
    TUBE_MODES,
    FunctionGenSettings,
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _settings(context) -> FunctionGenSettings:
    return context.scene.function_gen


def _formula_error(settings: FunctionGenSettings):
    """校验当前模式所有公式，返回 (是否有效, 错误信息)。"""
    try:
        if settings.mode in HEIGHT_MODES:
            from .formula import PARAMETERS, Formula

            defaults = settings.parameter_values()
            for index, source in enumerate(settings.height_formulas()):
                try:
                    Formula(
                        source,
                        parameters=PARAMETERS,
                        parameter_defaults=defaults,
                    )
                except FormulaError as exc:
                    label = "主公式" if index == 0 else f"附加公式 {index}"
                    return False, f"{label}：{exc}"
        else:
            settings.param_formulas_or_raise()
    except FormulaError as exc:
        return False, str(exc)
    return True, ""


def _effective_hint(settings: FunctionGenSettings) -> int:
    """当前设置下实际使用的采样分段数（自动加密后）。"""
    span = float(settings.range_end) - float(settings.range_start)
    if settings.mode == "PARAM_SURFACE":
        span = max(span, float(settings.range2_end) - float(settings.range2_start))
    is_surface = settings.mode in {"SURFACE", "PARAM_SURFACE"}
    try:
        effective, _ = generators.effective_segments(settings, span, is_surface=is_surface)
    except Exception:
        effective = int(settings.resolution)
    return effective


def _estimate_text(settings: FunctionGenSettings) -> str:
    effective = _effective_hint(settings)
    extra = f"（自动加密到 {effective} 段）" if effective != int(settings.resolution) else ""
    if settings.mode in HEIGHT_MODES:
        count = len(settings.height_formulas())
        per = (effective + 1) ** 2
        total = per * count
        text = f"预计顶点数：{total}（{count} 个物体 × {per}）{extra}"
        return text
    if settings.mode == "PARAM_CURVE":
        return f"预计路径点数：{effective + 1}{extra}"
    per_side = effective + 1
    return f"预计顶点数：{per_side * per_side}{extra}"


def _token_operator(layout, label: str, token: str):
    operator = layout.operator("functiongen.insert_formula_token", text=label)
    operator.token = token
    return operator


# ---------------------------------------------------------------------------
# 数学键盘（Desmos 风格输入，v1.2）
# ---------------------------------------------------------------------------

_KBD_TARGET_LABELS = {
    "MAIN": "主公式",
    "EXTRA": "附加公式",
    "CX": "x(t)",
    "CY": "y(t)",
    "CZ": "z(t)",
    "SX": "x(u,v)",
    "SY": "y(u,v)",
    "SZ": "z(u,v)",
}


def _kbd_target_name(settings: FunctionGenSettings) -> str:
    if settings.kbd_target == "EXTRA":
        return f"附加公式 {settings.kbd_extra_index + 1}"
    return _KBD_TARGET_LABELS.get(settings.kbd_target, settings.kbd_target)


def _kbd_variables(settings: FunctionGenSettings) -> tuple:
    if settings.kbd_target in {"CX", "CY", "CZ"}:
        return ("t",)
    if settings.kbd_target in {"SX", "SY", "SZ"}:
        return ("u", "v")
    return ("x", "y")


def _caret_display(settings: FunctionGenSettings) -> str:
    """显示「带光标的公式」；太长时只显示光标附近的一段。"""
    text = settings.kbd_text()
    pos = max(0, min(int(settings.kbd_pos), len(text)))
    if len(text) <= 42:
        return " " + text[:pos] + "|" + text[pos:] + " "
    start = max(0, pos - 26)
    end = min(len(text), pos + 16)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return " " + prefix + text[start:pos] + "|" + text[pos:end] + suffix + " "


def _draw_math_keypad(layout, settings: FunctionGenSettings) -> None:
    """Desmos 风格数学键盘：数字/运算符/变量/函数 + 光标移动/删除。"""
    box = layout.box()
    row = box.row(align=True)
    row.label(text=f"数学键盘 → {_kbd_target_name(settings)}", icon="NONE")
    close = row.operator("functiongen.math_key_action", text="关闭", icon="CANCEL")
    close.action = "CLOSE"

    column = box.column(align=True)
    column.scale_y = 1.25
    column.label(text=_caret_display(settings), icon="NONE")

    row = box.row(align=True)
    row.scale_y = 1.25
    row.operator("functiongen.math_key_functions", text="功能（函数 / 常量）", icon="PRESET")

    grid = box.grid_flow(row_major=True, columns=4, align=True, even_columns=True)

    def key(label: str, token: str, back: int = 0, skip: bool = False) -> None:
        operator = grid.operator("functiongen.math_key_token", text=label)
        operator.token = token
        operator.back = back
        operator.skip_closing = skip

    def action(label: str, action_name: str) -> None:
        operator = grid.operator("functiongen.math_key_action", text=label)
        operator.action = action_name

    for variable in _kbd_variables(settings):
        key(variable, variable)
    key("x²", "^2")
    key("xʸ", "^")

    key("(", "(")
    key(")", ")")
    key("<", "<", skip=True)
    key(">", ">", skip=True)

    key("|a|", "abs()", back=1)
    key(",", ",")
    key("≤", "<=", skip=True)
    key("≥", ">=", skip=True)

    key("π", "pi")
    key("√", "sqrt()", back=1)
    key("e", "e")
    key("÷", "/", skip=True)

    for label in ("7", "8", "9"):
        key(label, label)
    key("×", "*", skip=True)
    for label in ("4", "5", "6"):
        key(label, label)
    key("−", "-", skip=True)
    for label in ("1", "2", "3"):
        key(label, label)
    key("+", "+", skip=True)
    key("0", "0")
    key(".", ".")
    key("=", "=")
    key("%", "%")

    action("←", "LEFT")
    action("→", "RIGHT")
    action("⌫", "BACKSPACE")
    action("C", "CLEAR")

    row = box.row(align=True)
    row.scale_y = 1.3
    done = row.operator("functiongen.math_key_action", text="完成（关闭键盘）", icon="CHECKMARK")
    done.action = "CLOSE"


def _draw_formula_row(layout, settings, prop_name: str, slot: str, extra_index: int = 0):
    """一行公式输入框 + 数学键盘按钮 + ƒ 函数模块按钮。"""
    row = layout.row(align=True)
    row.prop(settings, prop_name, text="")
    keypad = row.operator("functiongen.math_key_open", text="⌨")
    keypad.target = slot
    keypad.extra_index = extra_index
    operator = row.operator("functiongen.formula_menu", text="ƒ", icon="PRESET")
    operator.target = slot
    operator.extra_index = extra_index


def _draw_formula_feedback(layout, settings: FunctionGenSettings) -> None:
    valid, message = _formula_error(settings)
    if not valid:
        box = layout.box()
        box.alert = True
        box.label(text=message, icon="ERROR")
    else:
        row = layout.row()
        row.label(text=_estimate_text(settings), icon="INFO")


def _draw_status(layout, settings: FunctionGenSettings) -> None:
    if not settings.last_message:
        return
    box = layout.box()
    box.alert = settings.last_error
    box.label(
        text=settings.last_message,
        icon="ERROR" if settings.last_error else "CHECKMARK",
    )


# ---------------------------------------------------------------------------
# 公式区（按模式显示不同输入框）
# ---------------------------------------------------------------------------

def _draw_formula_section(layout, settings: FunctionGenSettings) -> None:
    box = layout.box()
    box.label(text="公式", icon="NONE")
    if settings.mode in HEIGHT_MODES:
        _draw_formula_row(box, settings, "formula", "MAIN")
        if settings.extra_formulas:
            row = box.row()
            row.template_list(
                "FUNCTIONGEN_UL_formulas", "", settings, "extra_formulas",
                settings, "extra_formulas_index", rows=2,
            )
        row = box.row(align=True)
        row.operator("functiongen.formula_add", text="添加附加公式", icon="ADD")
        if settings.extra_formulas:
            row.operator("functiongen.formula_remove", text="删除", icon="REMOVE")
            row.operator("functiongen.formula_clear", text="清空", icon="TRASH")
        box.label(
            text="附加公式会一起生成不同物体（如 sin / cos / 乘积）",
            icon="INFO",
        )
    elif settings.mode == "PARAM_CURVE":
        _draw_formula_row(box, settings, "curve_x", "CX")
        _draw_formula_row(box, settings, "curve_y", "CY")
        _draw_formula_row(box, settings, "curve_z", "CZ")
        box.label(text="变量：t（点 ⌨ 打开数学键盘，或 ƒ 快速插入）", icon="INFO")
    else:
        _draw_formula_row(box, settings, "surf_x", "SX")
        _draw_formula_row(box, settings, "surf_y", "SY")
        _draw_formula_row(box, settings, "surf_z", "SZ")
        box.label(text="变量：u、v（点 ⌨ 打开数学键盘，或 ƒ 快速插入）", icon="INFO")
    box.label(
        text="Desmos 写法：2x^2+sin(πx)、y = …、|x|、√x、x≤1 都可以直接识别",
        icon="INFO",
    )
    _draw_formula_feedback(box, settings)
    if settings.kbd_active:
        _draw_math_keypad(box, settings)

    # 公式系数参数（v1.3）：静态版本用这里的值求值；几何节点版本是节点输入
    box.separator()
    box.label(text="公式参数（公式里可以直接写 a、b、c、d）", icon="NONE")
    row = box.row(align=True)
    row.prop(settings, "param_a")
    row.prop(settings, "param_b")
    row = box.row(align=True)
    row.prop(settings, "param_c")
    row.prop(settings, "param_d")
    box.label(
        text="几何节点版本里 a-d 是节点输入，可连噪声/场景时间/编号等字段让系数变化",
        icon="INFO",
    )


def _draw_range_section(layout, settings: FunctionGenSettings) -> None:
    box = layout.box()
    label = "采样范围"
    if settings.mode == "PARAM_SURFACE":
        label = "采样范围（U / V）"
    box.label(text=label, icon="NONE")
    row = box.row(align=True)
    row.prop(settings, "range_start", text="起始")
    row.prop(settings, "range_end", text="结束")
    if settings.mode == "PARAM_SURFACE":
        row = box.row(align=True)
        row.prop(settings, "range2_start", text="V 起始")
        row.prop(settings, "range2_end", text="V 结束")
    box.prop(settings, "resolution")
    box.prop(settings, "adaptive_sampling")
    if settings.mode in TUBE_MODES:
        box.prop(settings, "tube_radius")


# ---------------------------------------------------------------------------
# 外观 / 预览
# ---------------------------------------------------------------------------

def _draw_custom_colors(layout, settings: FunctionGenSettings) -> None:
    """自定义渐变色标编辑：颜色块 + 位置 + 删除；支持自己新建颜色。"""
    column = layout.column(align=True)
    column.label(text="自定义颜色（位置 0=起点，1=终点）", icon="COLOR")
    if len(settings.custom_colors) == 0:
        column.label(text="还没有自定义颜色，点「添加颜色」新建", icon="INFO")
    for index, item in enumerate(settings.custom_colors):
        row = column.row(align=True)
        row.prop(item, "color", text="")
        row.prop(item, "position", text="位置")
        operator = row.operator("functiongen.color_remove", text="", icon="REMOVE")
        operator.index = index
    row = column.row(align=True)
    row.operator("functiongen.color_add", text="添加颜色", icon="ADD")
    row.operator("functiongen.color_reset", text="恢复默认", icon="FILE_REFRESH")
    column.label(text="色标会自动按位置排序，用于渐变材质", icon="BLANK1")


def _draw_appearance(layout, settings: FunctionGenSettings) -> None:
    """v1.5：材质统一走 Blender 材质系统；这里只保留快捷操作。"""
    box = layout.box()
    box.label(text="外观 / 材质", icon="MATERIAL")
    row = box.row(align=True)
    row.prop(settings, "material_target", text="统一材质")
    row = box.row(align=True)
    row.operator("functiongen.material_new", text="新建材质", icon="MATERIAL")
    row.operator("functiongen.material_apply_colors", text="套用颜色", icon="COLOR")
    row = box.row(align=True)
    row.operator("functiongen.material_assign_target", text="应用到选中", icon="CHECKMARK")
    row.operator("functiongen.material_open_shader", text="着色器", icon="NODETREE")
    row = box.row(align=True)
    row.operator("functiongen.material_sync_node", text="物体材质→节点组", icon="NODETREE")
    row.operator("functiongen.material_use_selected", text="用选中物体材质", icon="IMPORT")
    box.prop(settings, "auto_material")
    try:
        active = bpy.context.active_object
        if active is not None and active.data is not None and len(active.data.materials):
            box.label(text="当前物体材质：" + active.data.materials[0].name, icon="MATERIAL")
    except Exception:
        pass
    box.label(
        text="无材质时自动创建；已有材质不覆盖；在 Shader Editor 里改色/改节点",
        icon="INFO",
    )

    if settings.auto_material or settings.material_target:
        quick = box.box()
        quick.label(text="快捷颜色（新建 / 套用材质时使用）", icon="COLOR")
        column = quick.column(align=True)
        column.prop(settings, "color_source")
        column.prop(settings, "palette")
        if settings.color_source == "SOLID":
            row = column.row(align=True)
            row.prop(settings, "solid_color", text="单色颜色")
        if settings.palette == "CUSTOM":
            _draw_custom_colors(column, settings)
        else:
            column.label(
                text="想快速生成自己的颜色：色带选「自定义颜色」，再点「新建材质/套用颜色」",
                icon="BLANK1",
            )


def _draw_preview(layout, settings: FunctionGenSettings) -> None:
    box = layout.box()
    box.label(text="渲染预览（自动取景 + 打光）", icon="NONE")
    row = box.row(align=True)
    row.scale_y = 1.3
    row.operator("functiongen.render_preview", text="渲染预览", icon="RENDER_STILL")
    row.operator("functiongen.open_preview", text="打开预览图", icon="IMAGE_DATA")
    if settings.preview_last_path:
        box.label(
            text="上次预览：" + os.path.basename(settings.preview_last_path),
            icon="FILE_IMAGE",
        )
    column = box.column(align=True)
    column.prop(settings, "preview_engine")
    row = column.row(align=True)
    row.prop(settings, "preview_resolution")
    row.prop(settings, "preview_samples")


# ---------------------------------------------------------------------------
# 3D 视图面板
# ---------------------------------------------------------------------------

class VIEW3D_PT_functiongen_main(bpy.types.Panel):
    """函数公式生成器主面板。"""

    bl_idname = "VIEW3D_PT_functiongen_main"
    bl_label = "函数公式生成器"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "函数生成"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        gn_nodes.maybe_schedule_sync(context)

        column = layout.column(align=True)
        column.prop(settings, "mode")

        _draw_formula_section(layout, settings)
        _draw_range_section(layout, settings)

        layout.separator()
        row = layout.row(align=True)
        row.scale_y = 1.5
        row.operator("functiongen.generate", text="生成", icon="ADD")

        row = layout.row(align=True)
        row.operator("functiongen.validate", text="校验公式", icon="CHECKMARK")
        row.menu("FUNCTIONGEN_MT_presets", text="示例公式", icon="PRESET")

        row = layout.row(align=True)
        row.operator("functiongen.load_selected", text="读取参数", icon="IMPORT")
        row.operator("functiongen.update_selected", text="更新选中", icon="FILE_REFRESH")

        _draw_appearance(layout, settings)
        _draw_preview(layout, settings)
        _draw_status(layout, settings)


class VIEW3D_PT_functiongen_advanced(bpy.types.Panel):
    """高级选项（默认折叠）。"""

    bl_idname = "VIEW3D_PT_functiongen_advanced"
    bl_label = "高级选项"
    bl_parent_id = "VIEW3D_PT_functiongen_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "函数生成"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        layout.prop(settings, "singular_handling")
        layout.prop(settings, "live_sync_gn")
        layout.prop(settings, "smooth_shading")
        layout.prop(settings, "at_cursor")
        if settings.mode in TUBE_MODES:
            layout.prop(settings, "convert_to_mesh")
            if settings.convert_to_mesh:
                column = layout.column(align=True)
                column.prop(settings, "curve_resolution")
                column.prop(settings, "bevel_resolution")
        if settings.mode == "PARAM_SURFACE":
            layout.prop(settings, "weld_seams")


class VIEW3D_PT_functiongen_nodes(bpy.types.Panel):
    """几何节点入口面板（3D 视图侧栏）。"""

    bl_idname = "VIEW3D_PT_functiongen_nodes"
    bl_label = "几何节点版本"
    bl_parent_id = "VIEW3D_PT_functiongen_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "函数生成"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        column = layout.column(align=True)
        column.label(text="生成可调参的非破坏性几何节点对象：", icon="INFO")
        column.operator(
            "functiongen.create_gn_object",
            text="生成几何节点对象",
            icon="NODETREE",
        )

        options = layout.box()
        options.label(text="节点组额外输入（生成后可在修改器里调）", icon="NONE")
        row = options.row(align=True)
        row.prop(settings, "gn_output_attributes")
        row.prop(settings, "smooth_shading")
        row = options.row(align=True)
        row.prop(settings, "adaptive_sampling")
        if settings.mode in TUBE_MODES:
            options.prop(settings, "fill_caps")
            options.prop(settings, "bevel_resolution")
        if settings.mode == "PARAM_SURFACE":
            options.prop(settings, "weld_seams")
        options.prop(settings, "gn_scale")
        options.prop(settings, "gn_offset")

        note = layout.column(align=True)
        note.label(text="也可以在几何节点编辑器里：", icon="BLANK1")
        note.label(text="Add 菜单 > 函数公式节点（几何体）/ 函数场节点。", icon="BLANK1")
        note.label(text="公式里的 a、b、c、d 是修改器/节点输入，可动画或连字段。", icon="BLANK1")
        _draw_status(layout, settings)


# ---------------------------------------------------------------------------
# 几何节点编辑器侧栏面板
# ---------------------------------------------------------------------------

class NODE_EDITOR_PT_functiongen_gn(bpy.types.Panel):
    """几何节点编辑器里的"函数生成"面板。"""

    bl_idname = "NODE_EDITOR_PT_functiongen_gn"
    bl_label = "函数公式生成器"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "函数生成"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return bool(
            space
            and space.type == "NODE_EDITOR"
            and space.tree_type == "GeometryNodeTree"
        )

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        layout.prop(settings, "mode")
        _draw_formula_section(layout, settings)
        _draw_range_section(layout, settings)
        _draw_appearance(layout, settings)

        layout.separator()
        column = layout.column(align=True)
        column.operator(
            "functiongen.create_gn_object",
            text="生成几何节点对象",
            icon="NODETREE",
        )
        column.scale_y = 1.2
        column.operator(
            "functiongen.add_gn_node",
            text="插入节点到当前树",
            icon="ADD",
        )

        layout.separator()
        layout.label(text="函数场节点（把公式当作字段使用）", icon="NONE")
        for kind, label, _description in (
            ("SCALAR", "函数值场（x,y→值）", ""),
            ("CURVE_VECTOR", "曲线向量场（t→xyz）", ""),
            ("SURFACE_VECTOR", "曲面向量场（u,v→xyz）", ""),
        ):
            operator = layout.operator("functiongen.add_gn_field", text=label)
            operator.kind = kind
        layout.operator("functiongen.update_field_nodes", text="更新节点树里的函数场", icon="FILE_REFRESH")
        layout.label(
            text="值场把 Position 接到「位置」输入；a-d 可连噪声/场景时间/编号等字段。",
            icon="INFO",
        )
        layout.label(text="公式会在插入时烘焙进节点，可在修改器里调范围/分辨率/半径。", icon="INFO")

        _draw_status(layout, settings)


# ---------------------------------------------------------------------------
# 批量公式列表
# ---------------------------------------------------------------------------

class FUNCTIONGEN_UL_formulas(bpy.types.UIList):
    """附加公式列表（每行一条公式 + ƒ 菜单）。"""

    bl_idname = "FUNCTIONGEN_UL_formulas"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "formula", text="", emboss=True)
        operator = row.operator("functiongen.math_key_open", text="⌨")
        operator.target = "EXTRA"
        operator.extra_index = index


# ---------------------------------------------------------------------------
# 示例公式菜单
# ---------------------------------------------------------------------------

class FUNCTIONGEN_MT_presets(bpy.types.Menu):
    """"示例公式"下拉菜单（按模式分组显示）。"""

    bl_idname = "FUNCTIONGEN_MT_presets"
    bl_label = "示例公式"

    def draw(self, context):
        layout = self.layout
        mode_labels = {item[0]: item[1] for item in MODE_ITEMS}
        first_group = True
        for mode in ("TUBE", "SURFACE", "PARAM_CURVE", "PARAM_SURFACE"):
            entries = [
                (index, data) for index, data in enumerate(PRESETS)
                if data.get("mode", "SURFACE") == mode
            ]
            if not entries:
                continue
            if not first_group:
                layout.separator()
            first_group = False
            layout.label(text=mode_labels.get(mode, mode))
            for index, data in entries:
                operator = layout.operator("functiongen.apply_preset", text=data["name"])
                operator.preset_index = index
        layout.separator()
        layout.label(text="示例只会填入参数，不会自动生成")


# ---------------------------------------------------------------------------
# 函数模块菜单（变量 / 常量 / 函数 / 运算符）
# ---------------------------------------------------------------------------

_TOKEN_TRIG = ("sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh")
_TOKEN_EXP = ("exp", "log", "ln", "log2", "log10", "sqrt", "pow", "hypot")
_TOKEN_ROUND = ("abs", "floor", "ceil", "round", "trunc", "sign")
_TOKEN_MISC = ("min", "max", "clamp", "degrees", "radians")


def _target_variables(settings) -> tuple:
    target = settings.formula_target
    if target in {"CX", "CY", "CZ"}:
        return ("t",)
    if target in {"SX", "SY", "SZ"}:
        return ("u", "v")
    return ("x", "y")


def _draw_token_items(layout, tokens, suffix: str = "") -> None:
    """垂直列出可点击的公式片段（菜单里不能使用 row/grid_flow，会叠字）。"""
    for token in tokens:
        operator = layout.operator("functiongen.insert_formula_token", text=token + suffix)
        operator.token = token + suffix


class FUNCTIONGEN_MT_tokens_trig(bpy.types.Menu):
    bl_idname = "FUNCTIONGEN_MT_tokens_trig"
    bl_label = "三角函数"

    def draw(self, context):
        _draw_token_items(self.layout, _TOKEN_TRIG, "(")


class FUNCTIONGEN_MT_tokens_exp(bpy.types.Menu):
    bl_idname = "FUNCTIONGEN_MT_tokens_exp"
    bl_label = "指数 / 对数 / 开方"

    def draw(self, context):
        _draw_token_items(self.layout, _TOKEN_EXP, "(")


class FUNCTIONGEN_MT_tokens_round(bpy.types.Menu):
    bl_idname = "FUNCTIONGEN_MT_tokens_round"
    bl_label = "取整 / 绝对值 / 符号"

    def draw(self, context):
        _draw_token_items(self.layout, _TOKEN_ROUND, "(")


class FUNCTIONGEN_MT_tokens_misc(bpy.types.Menu):
    bl_idname = "FUNCTIONGEN_MT_tokens_misc"
    bl_label = "其它函数"

    def draw(self, context):
        _draw_token_items(self.layout, _TOKEN_MISC, "(")


class FUNCTIONGEN_MT_formula_tokens(bpy.types.Menu):
    """函数模块菜单：点击把片段追加到目标公式输入框。"""

    bl_idname = "FUNCTIONGEN_MT_formula_tokens"
    bl_label = "函数模块"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        layout.label(text="变量")
        _draw_token_items(layout, _target_variables(settings))
        layout.label(text="常量")
        _draw_token_items(layout, ("pi", "e", "tau"))

        layout.separator()
        layout.label(text="函数（插入左括号，最后点 ) 补全）")
        layout.menu("FUNCTIONGEN_MT_tokens_trig", text="三角函数")
        layout.menu("FUNCTIONGEN_MT_tokens_exp", text="指数 / 对数 / 开方")
        layout.menu("FUNCTIONGEN_MT_tokens_round", text="取整 / 绝对值 / 符号")
        layout.menu("FUNCTIONGEN_MT_tokens_misc", text="其它函数（min / max / clamp …）")

        layout.separator()
        layout.label(text="运算符")
        for text, token in (
            ("加 +", " + "),
            ("减 -", " - "),
            ("乘 *", " * "),
            ("除 /", " / "),
            ("幂 **", " ** "),
            ("左括号 (", " ("),
            ("右括号 )", ")"),
            ("逗号 ,", ","),
        ):
            operator = layout.operator("functiongen.insert_formula_token", text=text)
            operator.token = token

        layout.separator()
        layout.label(text="当前插入目标：" + settings.formula_target)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

CLASSES = (
    VIEW3D_PT_functiongen_main,
    VIEW3D_PT_functiongen_advanced,
    VIEW3D_PT_functiongen_nodes,
    NODE_EDITOR_PT_functiongen_gn,
    FUNCTIONGEN_UL_formulas,
    FUNCTIONGEN_MT_presets,
    FUNCTIONGEN_MT_formula_tokens,
    FUNCTIONGEN_MT_tokens_trig,
    FUNCTIONGEN_MT_tokens_exp,
    FUNCTIONGEN_MT_tokens_round,
    FUNCTIONGEN_MT_tokens_misc,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)