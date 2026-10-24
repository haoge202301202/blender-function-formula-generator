# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""操作符模块：面板按钮背后的"动作"都在这里。

每个 Operator 都很薄：只负责取参数、调用 generators / gn_nodes / preview / formula，
把错误变成 Blender 状态栏提示（self.report）和面板状态（settings.last_message），
真正的逻辑在对应模块里，方便维护与 headless 测试。

操作符一览
==========
* functiongen.generate         生成（批量公式模式会生成多个物体）
* functiongen.validate         只校验公式（当前模式的全部公式）
* functiongen.load_selected    把选中物体的参数读回面板
* functiongen.update_selected  用当前面板参数重建选中物体（保留物体/材质/修改器）
* functiongen.apply_preset     应用示例公式（支持参数模式与批量示例）
* functiongen.formula_add / formula_remove / formula_clear   批量公式列表维护
* functiongen.color_add / color_remove / color_reset        自定义渐变颜色维护
* functiongen.material_new / material_apply_colors / material_assign_target /
  material_use_selected / material_open_shader              材质快捷操作（v1.5）
* functiongen.formula_menu     打开函数模块菜单（指定插入目标）
* functiongen.insert_formula_token  把函数片段插入到目标公式框（数学键盘模式下插到光标处）
* functiongen.math_key_open / math_key_token / math_key_action / math_key_functions
                                Desmos 风格数学键盘（v1.2）
* functiongen.open_preview     在图片编辑器打开上一次预览图
* functiongen.render_preview   一键渲染预览（自动在图片编辑器显示）

维护提示：新增按钮 → 在 ui.py 里 layout.operator(...) 引用新 operator；
bl_idname 统一用 "functiongen.xxx" 前缀。
"""
from __future__ import annotations

import traceback

import bpy

from . import generators, preview
from .formula import PARAMETERS, Formula, FormulaError, validate_text
from .generators import GenerationError
from .properties import (
    FORMULA_TARGET_ITEMS,
    HEIGHT_MODES,
    PARAM_MODES,
    PRESETS,
)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _status(operator, settings, message: str, is_error: bool = False) -> None:
    """统一的提示：面板状态 + Blender 状态栏。"""
    settings.set_status(message, is_error)
    operator.report({"ERROR"} if is_error else {"INFO"}, message)


def _object_summary(obj: bpy.types.Object) -> str:
    data = obj.data
    if isinstance(data, bpy.types.Mesh):
        return f"{len(data.vertices)} 个顶点 / {len(data.polygons)} 个面"
    if isinstance(data, bpy.types.Curve):
        points = sum(
            len(spline.bezier_points) if spline.type == "BEZIER" else len(spline.points)
            for spline in data.splines
        )
        return f"{len(data.splines)} 条样条 / {points} 个控制点"
    return str(data)


def _effective_count(obj) -> int:
    try:
        return int(obj.get("functiongen_effective_segments", 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _invalidate_message(settings, results) -> str:
    """拼出批量生成后的状态文字（含自动加密/无定义点提示）。"""
    count = len(results)
    invalid = sum(int(item[1]) for item in results)
    effective = max((_effective_count(obj) for obj, _ in results), default=0)
    if count == 1:
        message = f"已生成 {_object_summary(results[0][0])}"
    else:
        message = f"已生成 {count} 个物体"
    if effective > int(settings.resolution):
        message += f"；大范围自动加密采样至 {effective} 段"
    if invalid:
        message += f"；自动处理 {invalid} 个无定义点"
    return message


def _handle_error(operator, settings, exc: Exception) -> None:
    _status(operator, settings, str(exc) or exc.__class__.__name__, is_error=True)


def _validate_current_formulas(settings) -> None:
    """按当前模式校验全部公式；失败抛 FormulaError（带字段名）。"""
    if settings.mode in HEIGHT_MODES:
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
                raise FormulaError(f"{label} 错误：{exc}") from None
    else:
        settings.param_formulas_or_raise()


def _range_valid(settings) -> bool:
    if not (settings.range_start < settings.range_end):
        return False
    if settings.mode == "PARAM_SURFACE" and not (settings.range2_start < settings.range2_end):
        return False
    return True


# ---------------------------------------------------------------------------
# 生成 / 校验 / 读取 / 更新
# ---------------------------------------------------------------------------

class FUNCTIONGEN_OT_generate(bpy.types.Operator):
    """根据当前面板参数生成（支持批量公式与 4 种模式）"""

    bl_idname = "functiongen.generate"
    bl_label = "生成函数图形"
    bl_description = "根据公式与参数生成曲面/圆管/参数曲线/参数曲面，并自动选中新物体"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        try:
            results = generators.generate_batch(context, settings)
        except (FormulaError, GenerationError) as exc:
            _handle_error(self, settings, exc)
            return {"CANCELLED"}
        except Exception as exc:  # 兜底：任何意外都不应把 Blender 搞崩
            traceback.print_exc()
            _handle_error(self, settings, exc)
            return {"CANCELLED"}

        message = _invalidate_message(settings, results)
        _status(self, settings, message)
        return {"FINISHED"}


class FUNCTIONGEN_OT_validate(bpy.types.Operator):
    """检查当前模式所有公式（语法、函数名、安全性），不生成几何"""

    bl_idname = "functiongen.validate"
    bl_label = "校验公式"
    bl_description = "检查当前模式所有公式是否合法（语法、函数名、安全性）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.function_gen
        try:
            _validate_current_formulas(settings)
        except FormulaError as exc:
            _handle_error(self, settings, exc)
            return {"CANCELLED"}
        if not _range_valid(settings):
            _status(self, settings, "范围起始值必须小于结束值", is_error=True)
            return {"CANCELLED"}
        count = 1 if settings.mode in PARAM_MODES else len(settings.height_formulas())
        _status(self, settings, f"公式有效（{count} 条），范围合法")
        return {"FINISHED"}


class FUNCTIONGEN_OT_load_selected(bpy.types.Operator):
    """把选中物体的生成参数读回面板"""

    bl_idname = "functiongen.load_selected"
    bl_label = "读取选中物体参数"
    bl_description = "把本插件生成的选中物体的公式与参数读回面板，方便修改后重新生成"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return generators.is_generated_object(context.active_object)

    def execute(self, context):
        settings = context.scene.function_gen
        obj = context.active_object
        if not generators.read_settings_from_object(obj, settings):
            _handle_error(self, settings, GenerationError("选中的物体不是本插件生成的"))
            return {"CANCELLED"}
        _status(self, settings, f"已读取 {obj.name} 的参数")
        return {"FINISHED"}


class FUNCTIONGEN_OT_update_selected(bpy.types.Operator):
    """用当前面板参数重建选中物体（保留物体对象、材质与修改器）"""

    bl_idname = "functiongen.update_selected"
    bl_label = "更新选中物体"
    bl_description = "用当前面板参数重新生成几何数据，替换选中物体（保留物体本身、材质和修改器）"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return generators.is_generated_object(context.active_object)

    def execute(self, context):
        settings = context.scene.function_gen
        old_obj = context.active_object
        if not generators.is_generated_object(old_obj):
            _handle_error(self, settings, GenerationError("请先选中本插件生成的物体"))
            return {"CANCELLED"}

        # 几何节点物体：不做数据替换，而是重建节点组/同步修改器输入（v1.2）
        if generators.is_gn_object(old_obj):
            from . import gn_nodes

            try:
                gn_nodes.sync_gn_object(context, old_obj, settings, force=True)
            except (FormulaError, GenerationError) as exc:
                _handle_error(self, settings, exc)
                return {"CANCELLED"}
            except Exception as exc:
                traceback.print_exc()
                _handle_error(self, settings, f"更新几何节点物体失败：{exc}")
                return {"CANCELLED"}
            _status(self, settings, f"已同步几何节点物体：{old_obj.name}")
            return {"FINISHED"}

        stored_mode = old_obj.get(generators.OBJ_PROP_MODE)
        if stored_mode and stored_mode != settings.mode:
            _handle_error(
                self,
                settings,
                GenerationError("当前生成模式与选中物体不同，请直接重新生成"),
            )
            return {"CANCELLED"}

        try:
            # 用"当前面板参数"重建（与按钮说明一致；批量模式下会重建为当前面板主公式）
            placeholder, invalid = generators.generate_single(context, settings)
        except (FormulaError, GenerationError) as exc:
            _handle_error(self, settings, exc)
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            _handle_error(self, settings, exc)
            return {"CANCELLED"}

        new_data = placeholder.data
        old_data = old_obj.data
        old_material = None
        try:
            if old_data is not None and len(old_data.materials) and old_data.materials[0]:
                old_material = old_data.materials[0]
        except Exception:
            old_material = None
        if type(new_data) is not type(old_data):
            bpy.data.objects.remove(placeholder, do_unlink=True)
            _handle_error(
                self,
                settings,
                GenerationError("生成模式或“转换为网格”设置与选中物体不同，请直接重新生成"),
            )
            return {"CANCELLED"}

        old_obj.data = new_data
        placeholder.data = old_data
        for key, value in dict(placeholder.items()).items():
            try:
                old_obj[key] = value
            except (TypeError, ValueError):
                pass
        bpy.data.objects.remove(placeholder, do_unlink=True)
        if isinstance(old_data, bpy.types.Mesh):
            bpy.data.meshes.remove(old_data)
        elif isinstance(old_data, bpy.types.Curve):
            bpy.data.curves.remove(old_data)

        from . import materials

        try:
            if settings.material_target is not None:
                materials.assign_material(old_obj, settings.material_target)
            elif old_material is not None:
                # 保留用户原来在着色器里编辑的材质
                materials.assign_material(old_obj, old_material)
            else:
                materials.ensure_object_material(old_obj, settings)
        except Exception:
            traceback.print_exc()

        generators.select_only(context, old_obj)
        message = f"已更新 {old_obj.name}（{_object_summary(old_obj)}）"
        if invalid:
            message += f"；自动处理 {invalid} 个无定义点"
        _status(self, settings, message)
        return {"FINISHED"}


class FUNCTIONGEN_OT_apply_preset(bpy.types.Operator):
    """应用一个示例公式到面板参数"""

    bl_idname = "functiongen.apply_preset"
    bl_label = "应用示例"
    bl_description = "把示例公式、模式与范围填入面板（不会自动生成）"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: bpy.props.IntProperty(name="示例序号", default=0, min=0)

    def execute(self, context):
        settings = context.scene.function_gen
        index = max(0, min(len(PRESETS) - 1, int(self.preset_index)))
        data = PRESETS[index]
        settings.mode = data.get("mode", "SURFACE")
        if "formula" in data:
            settings.formula = data["formula"]
        if "curve" in data:
            settings.curve_x, settings.curve_y, settings.curve_z = data["curve"]
        if "surf" in data:
            settings.surf_x, settings.surf_y, settings.surf_z = data["surf"]
        if "start" in data:
            settings.range_start = float(data["start"])
        if "end" in data:
            settings.range_end = float(data["end"])
        if "range2" in data and data["range2"]:
            settings.range2_start = float(data["range2"][0])
            settings.range2_end = float(data["range2"][1])
        if "radius" in data:
            settings.tube_radius = float(data["radius"])
        if "resolution" in data:
            settings.resolution = int(data["resolution"])
        settings.adaptive_sampling = True
        settings.extra_formulas.clear()
        for source in data.get("extras", []):
            item = settings.extra_formulas.add()
            item.formula = source
        settings.extra_formulas_index = 0
        _status(self, settings, f"已填入示例：{data['name']}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 批量公式列表维护
# ---------------------------------------------------------------------------

def _selected_generated_objects(context):
    from . import generators

    result = []
    for obj in getattr(context, "selected_objects", []) or []:
        if generators.is_generated_object(obj):
            result.append(obj)
    if not result:
        active = getattr(context, "active_object", None)
        if generators.is_generated_object(active):
            result.append(active)
    return result


def _apply_material_to_object(obj, material) -> None:
    """对静态/几何节点物体都尽量把材质用上。"""
    from . import materials

    materials.assign_material(obj, material)
    if obj.modifiers:
        from . import generators, gn_nodes

        if generators.is_gn_object(obj):
            modifier = next((m for m in obj.modifiers if m.type == "NODES"), None)
            if modifier and modifier.node_group:
                gn_nodes._set_modifier_material(modifier, modifier.node_group, material)


class FUNCTIONGEN_OT_material_new(bpy.types.Operator):
    """新建一份可在着色器里编辑的材质"""

    bl_idname = "functiongen.material_new"
    bl_label = "新建材质"
    bl_description = "按当前颜色设置创建一份普通 Blender 材质，放到材质槽并可在 Shader Editor 里继续编辑"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import materials

        settings = context.scene.function_gen
        objects = _selected_generated_objects(context)
        obj = objects[0] if objects else getattr(context, "active_object", None)
        material = materials.create_material_from_settings(
            settings, "函数材质", obj=obj if obj is not None else None
        )
        settings.material_target = material
        for target in objects:
            _apply_material_to_object(target, material)
        settings.set_status(
            f"已新建材质「{material.name}」，可在 Shader Editor 里继续编辑"
        )
        return {"FINISHED"}


class FUNCTIONGEN_OT_material_apply_colors(bpy.types.Operator):
    """把面板里的快捷颜色套到当前材质"""

    bl_idname = "functiongen.material_apply_colors"
    bl_label = "套用颜色"
    bl_description = "把当前色带/自定义颜色/单色套到材质节点上（不会重建材质，其它着色节点保留）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import materials

        settings = context.scene.function_gen
        objects = _selected_generated_objects(context)
        material = settings.material_target
        if material is None and objects:
            try:
                if objects[0].data and len(objects[0].data.materials):
                    material = objects[0].data.materials[0]
            except Exception:
                material = None
        if material is None:
            material = materials.create_material_from_settings(
                settings, "函数材质", obj=objects[0] if objects else None
            )
            settings.material_target = material
        ok = materials.apply_colors_to_material(material, settings)
        for target in objects:
            _apply_material_to_object(target, material)
        if ok:
            settings.set_status(f"已把当前颜色套用到材质「{material.name}」")
        else:
            settings.set_status(
                f"材质「{material.name}」不是内置渐变材质，没有色带节点可套用；请直接在 Shader Editor 里改色",
                is_error=True,
            )
        return {"FINISHED"}


class FUNCTIONGEN_OT_material_assign_target(bpy.types.Operator):
    """把面板选中的材质应用到选中的本插件物体"""

    bl_idname = "functiongen.material_assign_target"
    bl_label = "应用材质到选中"
    bl_description = "把当前选中的材质应用到选中的本插件物体（静态和几何节点都支持）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        objects = _selected_generated_objects(context)
        material = settings.material_target
        if material is None and objects:
            try:
                if objects[0].data is not None and len(objects[0].data.materials):
                    material = objects[0].data.materials[0]
            except Exception:
                material = None
        if material is None:
            settings.set_status("请先选择一个材质，或选中已有材质的物体", is_error=True)
            self.report({"ERROR"}, "没有选择材质")
            return {"CANCELLED"}
        if not objects:
            settings.set_status("请选中本插件生成的物体", is_error=True)
            self.report({"ERROR"}, "没有选中本插件生成的物体")
            return {"CANCELLED"}
        for obj in objects:
            _apply_material_to_object(obj, material)
        settings.set_status(f"已把材质「{material.name}」应用到 {len(objects)} 个物体")
        return {"FINISHED"}


class FUNCTIONGEN_OT_material_use_selected(bpy.types.Operator):
    """读取当前选中物体的材质作为统一材质"""

    bl_idname = "functiongen.material_use_selected"
    bl_label = "用选中物体材质"
    bl_description = "把选中物体材质槽里的第一个材质设为统一材质，新生成物体也会用它"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        obj = getattr(context, "active_object", None)
        material = None
        try:
            if obj is not None and obj.data is not None and len(obj.data.materials):
                material = obj.data.materials[0]
        except Exception:
            material = None
        if material is None:
            settings.set_status("选中的物体没有材质", is_error=True)
            self.report({"ERROR"}, "选中的物体没有材质")
            return {"CANCELLED"}
        settings.material_target = material
        settings.set_status(f"已选用材质「{material.name}」")
        return {"FINISHED"}


class FUNCTIONGEN_OT_material_sync_node(bpy.types.Operator):
    """把物体的材质写入几何节点组的「材质」输入"""

    bl_idname = "functiongen.material_sync_node"
    bl_label = "物体材质→节点组"
    bl_description = "把当前物体材质槽里的材质写入它几何节点修改器的「材质」输入（自建物体+插件节点组时用）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import generators, gn_nodes

        settings = context.scene.function_gen
        obj = getattr(context, "active_object", None)
        if obj is None or obj.data is None or len(obj.data.materials) == 0:
            settings.set_status("当前物体没有材质，请先在材质属性里新建/指定一个", is_error=True)
            self.report({"ERROR"}, "没有材质")
            return {"CANCELLED"}
        material = obj.data.materials[0]
        modifier = next((m for m in obj.modifiers if m.type == "NODES"), None)
        if modifier is None or modifier.node_group is None:
            settings.set_status("当前物体没有几何节点修改器", is_error=True)
            self.report({"ERROR"}, "没有几何节点修改器")
            return {"CANCELLED"}
        gn_nodes._set_modifier_material(modifier, modifier.node_group, material)
        obj.update_tag()
        settings.set_status(f"已把材质「{material.name}」写入几何节点组")
        return {"FINISHED"}


class FUNCTIONGEN_OT_material_open_shader(bpy.types.Operator):
    """打开着色器编辑器编辑当前材质"""

    bl_idname = "functiongen.material_open_shader"
    bl_label = "编辑材质"
    bl_description = "在着色器编辑器里打开当前材质（没有着色器窗口时自动新建一个）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.function_gen
        material = settings.material_target
        if material is None:
            obj = getattr(context, "active_object", None)
            try:
                if obj is not None and obj.data is not None and len(obj.data.materials):
                    material = obj.data.materials[0]
            except Exception:
                material = None
        if material is None or material.node_tree is None:
            settings.set_status("当前没有可编辑的材质，请先点「新建材质」", is_error=True)
            self.report({"ERROR"}, "没有材质")
            return {"CANCELLED"}
        settings.material_target = material
        try:
            from . import material_editor

            material_editor.open_shader_editor(material)
        except Exception as exc:
            traceback.print_exc()
            settings.set_status(f"打开着色器编辑器失败：{exc}", is_error=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.set_status(f"已在 Shader Editor 中打开材质「{material.name}」")
        return {"FINISHED"}


class FUNCTIONGEN_OT_color_add(bpy.types.Operator):
    """在自定义渐变色标里新增一种颜色"""

    bl_idname = "functiongen.color_add"
    bl_label = "添加颜色"
    bl_description = "新增一个自定义颜色色标（颜色可以点取色器自己调）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import colorsys

        settings = context.scene.function_gen
        if settings.palette != "CUSTOM":
            settings.palette = "CUSTOM"
        if len(settings.custom_colors) == 0:
            settings.fill_custom_defaults()
            settings.set_status("已创建默认自定义颜色，点颜色块可以改成任意颜色")
            return {"FINISHED"}
        positions = sorted(float(item.position) for item in settings.custom_colors)
        edges = [0.0] + positions + [1.0]
        best_start, best_gap = 0.0, -1.0
        for left, right in zip(edges, edges[1:]):
            if right - left > best_gap:
                best_gap = right - left
                best_start = left
        position = min(1.0, max(0.0, best_start + best_gap * 0.5))
        index = len(settings.custom_colors)
        hue = (index * 0.17 + 0.55) % 1.0
        rgb = colorsys.hsv_to_rgb(hue, 0.75, 0.95)
        item = settings.custom_colors.add()
        item.position = position
        item.color = (*rgb, 1.0)
        settings.custom_colors_index = index
        settings.set_status(f"已添加颜色（位置 {position:.2f}），可拖动位置或点颜色块修改")
        return {"FINISHED"}


class FUNCTIONGEN_OT_color_remove(bpy.types.Operator):
    """删除一个自定义颜色色标"""

    bl_idname = "functiongen.color_remove"
    bl_label = "删除颜色"
    bl_description = "删除这个自定义颜色色标"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty(name="颜色序号", default=0, min=0)

    def execute(self, context):
        settings = context.scene.function_gen
        if not settings.custom_colors:
            return {"CANCELLED"}
        index = max(0, min(len(settings.custom_colors) - 1, int(self.index)))
        settings.custom_colors.remove(index)
        settings.custom_colors_index = max(
            0, min(index, len(settings.custom_colors) - 1)
        )
        settings.set_status("已删除一个自定义颜色")
        return {"FINISHED"}


class FUNCTIONGEN_OT_color_reset(bpy.types.Operator):
    """恢复默认的自定义颜色色标"""

    bl_idname = "functiongen.color_reset"
    bl_label = "恢复默认颜色"
    bl_description = "用蓝→青→黄的默认色标重置自定义颜色列表"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        settings.fill_custom_defaults()
        settings.palette = "CUSTOM"
        settings.set_status("已恢复默认自定义颜色（可继续增删/改色）")
        return {"FINISHED"}


class FUNCTIONGEN_OT_formula_add(bpy.types.Operator):
    """在列表末尾添加一条附加公式"""

    bl_idname = "functiongen.formula_add"
    bl_label = "添加公式"
    bl_description = "添加一条附加公式（批量生成时每条公式生成一个物体）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        item = settings.extra_formulas.add()
        item.formula = "cos(x) * 2"
        settings.extra_formulas_index = len(settings.extra_formulas) - 1
        _status(self, settings, f"已添加第 {len(settings.extra_formulas)} 条附加公式")
        return {"FINISHED"}


class FUNCTIONGEN_OT_formula_remove(bpy.types.Operator):
    """删除当前选中的附加公式"""

    bl_idname = "functiongen.formula_remove"
    bl_label = "删除公式"
    bl_description = "删除当前选中的附加公式"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        if not settings.extra_formulas:
            return {"CANCELLED"}
        index = max(0, min(len(settings.extra_formulas) - 1, settings.extra_formulas_index))
        settings.extra_formulas.remove(index)
        settings.extra_formulas_index = max(0, min(index, len(settings.extra_formulas) - 1))
        _status(self, settings, "已删除附加公式")
        return {"FINISHED"}


class FUNCTIONGEN_OT_formula_clear(bpy.types.Operator):
    """清空所有附加公式"""

    bl_idname = "functiongen.formula_clear"
    bl_label = "清空附加公式"
    bl_description = "删除全部附加公式，只保留主公式"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.function_gen
        settings.extra_formulas.clear()
        settings.extra_formulas_index = 0
        _status(self, settings, "已清空附加公式")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 函数模块（点击追加到公式）
# ---------------------------------------------------------------------------

class FUNCTIONGEN_OT_insert_formula_token(bpy.types.Operator):
    """把一个函数/变量/运算符片段追加到当前插入目标"""

    bl_idname = "functiongen.insert_formula_token"
    bl_label = "插入公式片段"
    bl_description = "把片段追加到当前公式输入框末尾"
    bl_options = {"REGISTER", "UNDO"}

    token: bpy.props.StringProperty(name="片段", default="")

    def execute(self, context):
        settings = context.scene.function_gen
        if not self.token:
            return {"CANCELLED"}
        if settings.kbd_active:
            token = self.token
            back = 0
            # 数学键盘模式下，函数菜单里的 "sin(" 变成 "sin()"，光标停在括号里
            stripped = token.rstrip()
            if stripped.endswith("(") and stripped.strip() != "(":
                token = stripped + ")"
                back = 1
            settings.kbd_insert(token, back=back)
            return {"FINISHED"}
        settings.append_to_target(self.token)
        return {"FINISHED"}


class FUNCTIONGEN_OT_formula_menu(bpy.types.Operator):
    """打开函数模块菜单（指定要插入的公式输入框）"""

    bl_idname = "functiongen.formula_menu"
    bl_label = "函数模块"
    bl_description = "选择函数/变量/运算符，追加到对应的公式输入框"
    bl_options = {"REGISTER"}

    target: bpy.props.EnumProperty(name="插入目标", items=FORMULA_TARGET_ITEMS, default="MAIN")
    extra_index: bpy.props.IntProperty(name="附加公式序号", default=0, min=0)

    def invoke(self, context, event):
        settings = context.scene.function_gen
        settings.formula_target = self.target
        settings.formula_extra_index = self.extra_index
        bpy.ops.wm.call_menu(name="FUNCTIONGEN_MT_formula_tokens")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 数学键盘（Desmos 风格输入，v1.2）
# ---------------------------------------------------------------------------

class FUNCTIONGEN_OT_math_key_open(bpy.types.Operator):
    """打开数学键盘，编辑指定公式框"""

    bl_idname = "functiongen.math_key_open"
    bl_label = "数学键盘"
    bl_description = "打开 Desmos 风格数学键盘，编辑对应的公式输入框"
    bl_options = {"REGISTER"}

    target: bpy.props.EnumProperty(name="目标公式", items=FORMULA_TARGET_ITEMS, default="MAIN")
    extra_index: bpy.props.IntProperty(name="附加公式序号", default=0, min=0)

    def execute(self, context):
        settings = context.scene.function_gen
        settings.kbd_open(self.target, self.extra_index)
        return {"FINISHED"}


class FUNCTIONGEN_OT_math_key_token(bpy.types.Operator):
    """在数学键盘光标处插入片段"""

    bl_idname = "functiongen.math_key_token"
    bl_label = "键盘按键"
    bl_description = "把按键内容插入到公式光标处"
    bl_options = {"REGISTER", "UNDO"}

    token: bpy.props.StringProperty(name="片段", default="")
    back: bpy.props.IntProperty(name="光标回退", default=0, min=0)
    skip_closing: bpy.props.BoolProperty(name="跳过右括号", default=False)
    close_after: bpy.props.BoolProperty(name="插入后关闭", default=False)

    def execute(self, context):
        settings = context.scene.function_gen
        settings.kbd_insert(self.token, back=self.back, skip_closing=self.skip_closing)
        if self.close_after:
            settings.kbd_close()
        return {"FINISHED"}


class FUNCTIONGEN_OT_math_key_action(bpy.types.Operator):
    """数学键盘控制键（移动/删除/清空/关闭）"""

    bl_idname = "functiongen.math_key_action"
    bl_label = "键盘操作"
    bl_description = "数学键盘的控制操作"
    bl_options = {"REGISTER", "UNDO"}

    action: bpy.props.EnumProperty(
        name="操作",
        items=(
            ("LEFT", "左移", ""),
            ("RIGHT", "右移", ""),
            ("HOME", "移到开头", ""),
            ("END", "移到末尾", ""),
            ("BACKSPACE", "退格", ""),
            ("DELETE", "删除", ""),
            ("CLEAR", "清空", ""),
            ("CLOSE", "关闭键盘", ""),
        ),
        default="RIGHT",
    )

    def execute(self, context):
        settings = context.scene.function_gen
        action = self.action
        if action == "LEFT":
            settings.kbd_move(-1)
        elif action == "RIGHT":
            settings.kbd_move(1)
        elif action == "HOME":
            settings.kbd_home()
        elif action == "END":
            settings.kbd_end()
        elif action == "BACKSPACE":
            settings.kbd_backspace()
        elif action == "DELETE":
            settings.kbd_delete()
        elif action == "CLEAR":
            settings.kbd_clear()
        elif action == "CLOSE":
            settings.kbd_close()
        return {"FINISHED"}


class FUNCTIONGEN_OT_math_key_functions(bpy.types.Operator):
    """数学键盘的「功能」菜单（插入函数）"""

    bl_idname = "functiongen.math_key_functions"
    bl_label = "功能"
    bl_description = "打开函数模块菜单，把函数插入到数学键盘光标处"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        settings = context.scene.function_gen
        settings.formula_target = settings.kbd_target
        settings.formula_extra_index = settings.kbd_extra_index
        bpy.ops.wm.call_menu(name="FUNCTIONGEN_MT_formula_tokens")
        return {"FINISHED"}


class FUNCTIONGEN_OT_open_preview(bpy.types.Operator):
    """在图片编辑器里打开上一次渲染的预览图"""

    bl_idname = "functiongen.open_preview"
    bl_label = "打开预览图"
    bl_description = "打开上一次渲染预览的图片（自动在图片编辑器中显示）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.function_gen
        path = getattr(settings, "preview_last_path", "")
        if not path:
            _handle_error(self, settings, "还没有渲染过预览图，请先点「渲染预览」")
            return {"CANCELLED"}
        try:
            shown = preview.open_preview_image(context, path)
        except Exception as exc:
            traceback.print_exc()
            _handle_error(self, settings, f"打开预览图失败：{exc}")
            return {"CANCELLED"}
        if shown:
            _status(self, settings, "已在图片编辑器中打开上次的预览图")
        else:
            _status(self, settings, f"预览图文件：{path}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 一键渲染预览
# ---------------------------------------------------------------------------



class FUNCTIONGEN_OT_render_preview(bpy.types.Operator):
    """自动摆相机/灯光渲染当前选中物体的预览图"""

    bl_idname = "functiongen.render_preview"
    bl_label = "渲染预览"
    bl_description = "自动取景与打光，渲染当前选中函数图形的预览图（不会改动场景设置）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.function_gen
        try:
            result = preview.render_preview(context, settings)
        except (preview.PreviewError, RuntimeError) as exc:
            _handle_error(self, settings, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            _handle_error(self, settings, f"渲染预览失败：{exc}")
            return {"CANCELLED"}
        settings.preview_last_path = result["path"]
        message = (
            f"预览已渲染（{result['engine']}，{result['resolution'][0]}x{result['resolution'][1]}）"
        )
        if result.get("displayed"):
            message += "，已在图片编辑器显示"
        message += f"；文件：{result['path']}"
        if result.get("fallback"):
            message += "（EEVEE 不可用，已自动降级到 Cycles）"
        _status(self, settings, message)
        return {"FINISHED"}


# 注册顺序（__init__.py 统一调用）
CLASSES = (
    FUNCTIONGEN_OT_generate,
    FUNCTIONGEN_OT_validate,
    FUNCTIONGEN_OT_load_selected,
    FUNCTIONGEN_OT_update_selected,
    FUNCTIONGEN_OT_apply_preset,
    FUNCTIONGEN_OT_material_new,
    FUNCTIONGEN_OT_material_apply_colors,
    FUNCTIONGEN_OT_material_assign_target,
    FUNCTIONGEN_OT_material_use_selected,
    FUNCTIONGEN_OT_material_sync_node,
    FUNCTIONGEN_OT_material_open_shader,
    FUNCTIONGEN_OT_color_add,
    FUNCTIONGEN_OT_color_remove,
    FUNCTIONGEN_OT_color_reset,
    FUNCTIONGEN_OT_formula_add,
    FUNCTIONGEN_OT_formula_remove,
    FUNCTIONGEN_OT_formula_clear,
    FUNCTIONGEN_OT_insert_formula_token,
    FUNCTIONGEN_OT_formula_menu,
    FUNCTIONGEN_OT_math_key_open,
    FUNCTIONGEN_OT_math_key_token,
    FUNCTIONGEN_OT_math_key_action,
    FUNCTIONGEN_OT_math_key_functions,
    FUNCTIONGEN_OT_open_preview,
    FUNCTIONGEN_OT_render_preview,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)