# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""自动渐变材质模块（v1.1 新增）。

给生成的函数图像套一套按数值渐变的材质：
    纹理坐标(Object) → 分离 XYZ → 取"高度 Z"或"到中心距离"
    → Map Range（按物体实测 min/max 归一化）→ ColorRamp（色带）→ Principled BSDF

设计说明
========
* 材质是**每个物体一份**（因为 min/max 因物体而异），命名带物体名，方便在材质面板里找；
* 渐变量全部由节点计算，几何节点生成的物体也一样生效（材质挂在物体上，与几何来源无关）；
* 色带定义在 PALETTES 里，想加配色只需加一条（stops 为 0~1 位置和 RGB 颜色）。

维护提示
========
* 新色带：在 PALETTES 里加一项，并在 properties.PALETTE_ITEMS 加同名枚举；
* 新着色依据：在 properties.COLOR_SOURCE_ITEMS 加枚举 + 在 _value_socket() 里实现取值节点；
* 如果以后要"批量共享材质"，需要把 min/max 改成属性驱动（当前按物体各建一份最简单可靠）。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import bpy
from mathutils import Vector

# 色带定义：{id: [(位置0~1, (r,g,b)), ...]}
PALETTES: Dict[str, List[Tuple[float, Tuple[float, float, float]]]] = {
    "RAINBOW": [
        (0.0, (0.08, 0.18, 0.90)),
        (0.25, (0.00, 0.80, 0.90)),
        (0.5, (0.10, 0.90, 0.25)),
        (0.75, (1.00, 0.85, 0.10)),
        (1.0, (0.95, 0.12, 0.08)),
    ],
    "WARM_COOL": [
        (0.0, (0.05, 0.15, 0.80)),
        (0.5, (0.95, 0.95, 0.95)),
        (1.0, (0.90, 0.08, 0.04)),
    ],
    "MONO": [
        (0.0, (0.03, 0.03, 0.03)),
        (1.0, (0.95, 0.95, 0.95)),
    ],
    "OCEAN": [
        (0.0, (0.01, 0.05, 0.26)),
        (0.4, (0.00, 0.48, 0.72)),
        (0.7, (0.18, 0.86, 0.76)),
        (1.0, (1.00, 0.95, 0.55)),
    ],
}


# ---------------------------------------------------------------------------
# 几何位置读取（用于计算 min/max）
# ---------------------------------------------------------------------------

def _object_positions(obj: bpy.types.Object) -> List[Vector]:
    """取物体（含修改器求值后）的局部坐标点，用于计算渐变范围。"""
    if obj.type == "MESH":
        if obj.modifiers:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                positions = [vertex.co.copy() for vertex in mesh.vertices]
            finally:
                evaluated.to_mesh_clear()
            if positions:
                return positions
        return [vertex.co.copy() for vertex in obj.data.vertices]
    if obj.type == "CURVE":
        positions = []
        for spline in obj.data.splines:
            if spline.type == "BEZIER":
                positions.extend(point.co.copy() for point in spline.bezier_points)
            else:
                positions.extend(Vector(point.co[:3]) for point in spline.points)
        return positions
    return []


def value_range(obj: bpy.types.Object, source: str = "Z") -> Tuple[float, float]:
    """返回用于渐变的数值范围 (min, max)。"""
    if source == "SOLID":
        return 0.0, 1.0
    positions = _object_positions(obj)
    if not positions:
        return 0.0, 1.0
    if source == "RADIAL":
        values = [Vector((p.x, p.y, 0.0)).length for p in positions]
    else:  # Z
        values = [p.z for p in positions]
    low = min(values)
    high = max(values)
    if high - low < 1e-9:
        high = low + 1.0
    return float(low), float(high)


# ---------------------------------------------------------------------------
# 材质节点搭建
# ---------------------------------------------------------------------------

def _value_socket(node_tree, tex_coord, separate, source: str):
    """返回"用于渐变的数值"socket：Z / 到中心距离 / 常量。"""
    if source == "RADIAL":
        combine = node_tree.nodes.new("ShaderNodeCombineXYZ")
        node_tree.links.new(separate.outputs[0], combine.inputs[0])
        node_tree.links.new(separate.outputs[1], combine.inputs[1])
        combine.inputs[2].default_value = 0.0
        length = node_tree.nodes.new("ShaderNodeVectorMath")
        length.operation = "LENGTH"
        node_tree.links.new(combine.outputs[0], length.inputs[0])
        return length.outputs[1]
    if source == "SOLID":
        value = node_tree.nodes.new("ShaderNodeValue")
        value.outputs[0].default_value = 0.5
        return value.outputs[0]
    # 默认：高度 Z
    return separate.outputs[2]


def _custom_stops_from_settings(settings):
    """读取面板里的自定义色标；没有就返回 None（用内置色带兜底）。"""
    getter = getattr(settings, "custom_palette_stops", None)
    if callable(getter):
        try:
            stops = getter()
            if stops:
                return stops
        except Exception:
            pass
    return None


def color_signature(settings) -> str:
    """材质参数签名：用于几何节点实时同步判断颜色是否需要重建。"""
    source = getattr(settings, "color_source", "Z")
    palette = getattr(settings, "palette", "RAINBOW")
    solid = tuple(round(float(c), 6) for c in getattr(settings, "solid_color", (0.1, 0.55, 1.0, 1.0)))
    stops = []
    getter = getattr(settings, "custom_palette_stops", None)
    if callable(getter):
        try:
            stops = [(round(float(pos), 6), tuple(round(float(c), 6) for c in color)) for pos, color in getter()]
        except Exception:
            stops = []
    return repr((source, palette, solid, stops))


def _set_ramp_stops(ramp: bpy.types.ColorRamp, stops) -> None:
    """把 ColorRamp 的色标设置为给定列表（保持渐变节点其它设置不变）。"""
    stops = sorted(stops, key=lambda pair: pair[0])
    elements = ramp.elements
    # 保留前两个色标作为端点，删掉多余的
    while len(elements) > 2:
        elements.remove(elements[-1])
    elements[0].position = float(stops[0][0])
    elements[0].color = (*stops[0][1], 1.0)
    elements[1].position = float(stops[-1][0])
    elements[1].color = (*stops[-1][1], 1.0)
    for position, color in stops[1:-1]:
        element = elements.new(float(position))
        element.color = (*color, 1.0)


def build_gradient_material(
    name: str,
    palette: str,
    value_min: float,
    value_max: float,
    source: str = "Z",
    custom_stops=None,
    solid_color=None,
) -> bpy.types.Material:
    """创建一份带节点渐变的材质。

    palette="CUSTOM" 时使用 custom_stops（面板里的自定义颜色）；source="SOLID" 时
    使用 solid_color 作为唯一颜色，方便用户自己新建任意纯色。
    """
    if palette == "CUSTOM" and custom_stops:
        stops = list(custom_stops)
        stops.sort(key=lambda pair: pair[0])
        if len(stops) == 1:
            stops = [(0.0, stops[0][1]), (1.0, stops[0][1])]
    else:
        stops = PALETTES.get(palette) or PALETTES["RAINBOW"]
    material = bpy.data.materials.new(name)
    node_tree = material.node_tree
    if node_tree is None:  # Blender 5.x 新材质默认带节点；旧版本才需要打开 use_nodes
        material.use_nodes = True
        node_tree = material.node_tree
    node_tree.nodes.clear()

    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (600.0, 0.0)
    bsdf = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (300.0, 0.0)
    bsdf.inputs["Roughness"].default_value = 0.35
    try:
        bsdf.inputs["Metallic"].default_value = 0.0
    except KeyError:
        pass

    # 单色模式：直接给一个可自定义的纯色，不建渐变节点
    if source == "SOLID":
        color = tuple(solid_color) if solid_color else (*stops[len(stops) // 2][1], 1.0)
        if len(color) == 3:
            color = (*color, 1.0)
        try:
            bsdf.inputs["Base Color"].default_value = color
        except Exception:
            pass
        node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
        material.diffuse_color = color
        return material

    tex_coord = node_tree.nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-800.0, 0.0)
    separate = node_tree.nodes.new("ShaderNodeSeparateXYZ")
    separate.location = (-600.0, 0.0)
    node_tree.links.new(tex_coord.outputs["Object"], separate.inputs[0])

    value_socket = _value_socket(node_tree, tex_coord, separate, source)
    value_socket.node.location = (-400.0, -100.0)

    map_range = node_tree.nodes.new("ShaderNodeMapRange")
    map_range.location = (-200.0, 0.0)
    map_range.clamp = True
    node_tree.links.new(value_socket, map_range.inputs[0])
    map_range.inputs[1].default_value = 0.0
    map_range.inputs[2].default_value = 1.0
    map_range.inputs[3].default_value = 0.0
    map_range.inputs[4].default_value = 1.0
    # 用"物体实际 min/max"驱动归一化
    map_range.inputs[1].default_value = value_min
    map_range.inputs[2].default_value = value_max

    ramp = node_tree.nodes.new("ShaderNodeValToRGB")
    ramp.location = (0.0, 0.0)
    color_ramp = ramp.color_ramp
    color_ramp.interpolation = "EASE"
    elements = color_ramp.elements
    # 先调整已有两个色标的颜色，再按需添加中间色标
    elements[0].position = stops[0][0]
    elements[0].color = (*stops[0][1], 1.0)
    elements[1].position = stops[-1][0]
    elements[1].color = (*stops[-1][1], 1.0)
    for position, color in stops[1:-1]:
        element = elements.new(position)
        element.color = (*color, 1.0)
    node_tree.links.new(map_range.outputs[0], ramp.inputs[0])

    node_tree.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    # 实体视图（Solid）里也能看到大致颜色
    mid = stops[len(stops) // 2][1]
    material.diffuse_color = (mid[0], mid[1], mid[2], 1.0)
    return material


def apply_auto_material(obj: bpy.types.Object, settings) -> bpy.types.Material:
    """给物体应用（或重建）自动渐变材质，返回材质对象。"""
    source = getattr(settings, "color_source", "Z")
    palette = getattr(settings, "palette", "RAINBOW")
    custom_stops = _custom_stops_from_settings(settings) if palette == "CUSTOM" else None
    solid_color = getattr(settings, "solid_color", None)
    low, high = value_range(obj, source)
    material = build_gradient_material(
        name=f"{obj.name}·渐变",
        palette=palette,
        value_min=low,
        value_max=high,
        source=source,
        custom_stops=custom_stops,
        solid_color=tuple(solid_color) if solid_color else None,
    )
    data = obj.data
    if data is not None:
        data.materials.clear()
        data.materials.append(material)
    obj.color = material.diffuse_color
    return material


# 别名：更新物体时也叫它，语义更清楚
refresh_material = apply_auto_material

# ---------------------------------------------------------------------------
# v1.5：材质统一交给 Blender 材质系统（着色器里编辑）
# ---------------------------------------------------------------------------

def _settings_stops(settings):
    palette = getattr(settings, "palette", "RAINBOW")
    if palette == "CUSTOM":
        getter = getattr(settings, "custom_palette_stops", None)
        if callable(getter):
            try:
                stops = getter()
                if stops:
                    return list(stops)
            except Exception:
                pass
    return PALETTES.get(palette) or PALETTES["RAINBOW"]


def assign_material(obj: bpy.types.Object, material: bpy.types.Material) -> bool:
    """把材质放进物体材质槽（不重建材质本身）。"""
    if obj is None or material is None or obj.data is None:
        return False
    try:
        slots = obj.data.materials
        if len(slots) == 0 or slots[0] != material:
            slots.clear()
            slots.append(material)
        obj.color = material.diffuse_color
        return True
    except Exception:
        return False


def create_material_from_settings(settings, name: str, obj=None) -> bpy.types.Material:
    """按当前颜色设置创建一份普通 Blender 材质（可继续在 Shader Editor 里编辑）。"""
    source = getattr(settings, "color_source", "Z")
    palette = getattr(settings, "palette", "RAINBOW")
    custom_stops = _settings_stops(settings) if palette == "CUSTOM" else None
    solid = getattr(settings, "solid_color", None)
    if obj is not None:
        low, high = value_range(obj, source)
    else:
        low, high = -1.0, 1.0
    return build_gradient_material(
        name=name,
        palette=palette,
        value_min=low,
        value_max=high,
        source=source,
        custom_stops=custom_stops,
        solid_color=tuple(solid) if solid else None,
    )


def ensure_object_material(obj: bpy.types.Object, settings):
    """返回物体应使用的材质；没有则按设置自动创建一份。

    优先级：面板指定的 material_target → 物体已有材质槽 → 自动创建。
    这样用户在着色器里改过的材质、手动换过的材质不会被覆盖。
    """
    target = getattr(settings, "material_target", None)
    if target is not None:
        assign_material(obj, target)
        return target
    try:
        if obj.data is not None and len(obj.data.materials) and obj.data.materials[0]:
            return obj.data.materials[0]
    except Exception:
        pass
    if not getattr(settings, "auto_material", True):
        return None
    material = create_material_from_settings(settings, f"{obj.name}·材质", obj)
    assign_material(obj, material)
    return material


def apply_colors_to_material(material: bpy.types.Material, settings) -> bool:
    """把面板里的颜色（色带/自定义色/单色）套到已有材质的节点上。

    只改材质节点里的颜色，不重建材质；返回是否成功（材质没有色带节点时失败）。
    """
    if material is None or material.node_tree is None:
        return False
    source = getattr(settings, "color_source", "Z")
    if source == "SOLID":
        solid = getattr(settings, "solid_color", None)
        color = tuple(solid) if solid else (0.8, 0.8, 0.8, 1.0)
        bsdf = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            return False
        try:
            bsdf.inputs["Base Color"].default_value = color
        except Exception:
            return False
        material.diffuse_color = color
        return True
    ramp = next((n for n in material.node_tree.nodes if n.type == "VALTORGB"), None)
    if ramp is None:
        return False
    stops = _settings_stops(settings)
    _set_ramp_stops(ramp.color_ramp, stops)
    mid = stops[len(stops) // 2][1]
    material.diffuse_color = (mid[0], mid[1], mid[2], 1.0)
    return True
