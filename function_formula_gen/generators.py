# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""几何生成模块：把公式采样结果变成 Blender 物体。

支持 4 种模式
=============
* TUBE          高度场圆管：沿 Y 轴路径，Z = f(x)，用曲线 Bevel 倒角；
* SURFACE       高度场曲面：XY 网格，Z = f(x, y)；
* PARAM_CURVE   参数曲线圆管：x(t), y(t), z(t)，可生成螺旋线、结等空间曲线；
* PARAM_SURFACE 参数曲面：x(u,v), y(u,v), z(u,v)，可生成球面、环面等真三维曲面。

关键处理（v1.1）
================
* **无定义点容错**：像 1/x（x=0）、log(0)、tan(90°) 这类点会让公式求值失败。
  默认 AUTO 模式：
    - 曲线：在无定义处把路径断开成多段样条（1/x 会得到左右两段圆管）；
    - 曲面：用同一行的邻近有效值填充该点，保证网格完整（会提示处理了几个点）。
  STRICT 模式则立即报错并给出坐标（可在高级选项切换）。
* **自动材质**：生成后调用 materials.apply_auto_material() 上渐变色；
* **批量公式**：TUBE/SURFACE 模式下主公式 + 附加公式列表逐条生成；
* **接缝焊接**：参数曲面首尾重合的顶点可自动焊起来（球面/环面）。

维护提示
========
* 新增模式：实现 _build_xxx(context, settings, sources, ...) 返回物体，
  在 MODE_BUILDERS 注册；同时更新 properties.MODE_ITEMS / ui.py / gn_nodes.py。
* 物体上的自定义属性 key 见 OBJ_PROP_*；新增参数时记得同步 OBJECT_PROP_KEYS，
  这样"读取参数/更新选中物体"才会生效。
"""
from __future__ import annotations

import json
import math
import traceback
from typing import Dict, List, Optional, Sequence, Tuple

import bpy
from mathutils import Vector

from . import formula as formula_module
from . import materials
from .formula import (
    PARAM_CURVE_VARIABLES,
    PARAM_SURFACE_VARIABLES,
    PARAMETERS,
    Formula,
    FormulaError,
)
from .properties import FunctionGenSettings, HEIGHT_MODES, PARAM_MODES

# 物体自定义属性名（用于识别"这是本插件生成的物体"以及参数回读）
OBJ_PROP_MARKER = "functiongen_generated"
OBJ_PROP_VERSION = "functiongen_version"
OBJ_PROP_MODE = "functiongen_mode"
OBJ_PROP_FORMULA = "functiongen_formula"
OBJ_PROP_EXTRA_FORMULAS = "functiongen_extra_formulas"
OBJ_PROP_START = "functiongen_range_start"
OBJ_PROP_END = "functiongen_range_end"
OBJ_PROP_RANGE2_START = "functiongen_range2_start"
OBJ_PROP_RANGE2_END = "functiongen_range2_end"
OBJ_PROP_RESOLUTION = "functiongen_resolution"
OBJ_PROP_RADIUS = "functiongen_tube_radius"
OBJ_PROP_CONVERT = "functiongen_convert_to_mesh"
OBJ_PROP_CURVE_RES = "functiongen_curve_resolution"
OBJ_PROP_BEVEL_RES = "functiongen_bevel_resolution"
OBJ_PROP_SMOOTH = "functiongen_smooth_shading"
OBJ_PROP_WELD = "functiongen_weld_seams"
OBJ_PROP_AUTO_MATERIAL = "functiongen_auto_material"
OBJ_PROP_COLOR_SOURCE = "functiongen_color_source"
OBJ_PROP_PALETTE = "functiongen_palette"
OBJ_PROP_SINGULAR = "functiongen_singular_handling"
OBJ_PROP_COLOR_SIG = "functiongen_color_signature"
OBJ_PROP_INVALID = "functiongen_invalid_points"
OBJ_PROP_EFFECTIVE = "functiongen_effective_segments"

# 自动加密采样参数（v1.1）：
#   大范围时按 "resolution × 范围/10" 提高分段数，小范围时保持用户设置不变。
#   10 是默认范围（-5~5）的跨度，所以默认参数下行为与 v1.0 完全一致。
_REFERENCE_SPAN = 10.0
_MAX_CURVE_SEGMENTS = 8000
_MAX_SURFACE_SEGMENTS = 512
OBJ_PROP_CURVE_X = "functiongen_curve_x"
OBJ_PROP_CURVE_Y = "functiongen_curve_y"
OBJ_PROP_CURVE_Z = "functiongen_curve_z"
OBJ_PROP_SURF_X = "functiongen_surf_x"
OBJ_PROP_SURF_Y = "functiongen_surf_y"
OBJ_PROP_SURF_Z = "functiongen_surf_z"
OBJ_PROP_GN = "functiongen_gn_object"

ADDON_VERSION = 2

# 保存到物体上的参数 → 参数组属性名（"读取选中物体参数"按这个表回读）
OBJECT_PROP_KEYS: Dict[str, str] = {
    OBJ_PROP_MODE: "mode",
    OBJ_PROP_START: "range_start",
    OBJ_PROP_END: "range_end",
    OBJ_PROP_RANGE2_START: "range2_start",
    OBJ_PROP_RANGE2_END: "range2_end",
    OBJ_PROP_RESOLUTION: "resolution",
    OBJ_PROP_RADIUS: "tube_radius",
    OBJ_PROP_CONVERT: "convert_to_mesh",
    OBJ_PROP_CURVE_RES: "curve_resolution",
    OBJ_PROP_BEVEL_RES: "bevel_resolution",
    OBJ_PROP_SMOOTH: "smooth_shading",
    OBJ_PROP_WELD: "weld_seams",
    OBJ_PROP_AUTO_MATERIAL: "auto_material",
    OBJ_PROP_COLOR_SOURCE: "color_source",
    OBJ_PROP_PALETTE: "palette",
    OBJ_PROP_SINGULAR: "singular_handling",
    OBJ_PROP_CURVE_X: "curve_x",
    OBJ_PROP_CURVE_Y: "curve_y",
    OBJ_PROP_CURVE_Z: "curve_z",
    OBJ_PROP_SURF_X: "surf_x",
    OBJ_PROP_SURF_Y: "surf_y",
    OBJ_PROP_SURF_Z: "surf_z",
}

# 生成模式的中文名（用于物体命名与提示）
MODE_LABELS = {
    "TUBE": "函数圆管",
    "SURFACE": "函数曲面",
    "PARAM_CURVE": "参数曲线管",
    "PARAM_SURFACE": "参数曲面",
}


class GenerationError(ValueError):
    """几何生成失败（参数不合法 / 结果全为无定义等）。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _cursor_location(context) -> Vector:
    """当前 3D 光标位置（生成物的默认落点）。"""
    try:
        return context.scene.cursor.location.copy()
    except AttributeError:
        return Vector((0.0, 0.0, 0.0))


def _link_object(context, obj: bpy.types.Object) -> None:
    context.collection.objects.link(obj)


def _select_only(context, obj: bpy.types.Object) -> None:
    """取消其它物体的选择，只选中 obj 并设为活动物体。"""
    view_layer = context.view_layer
    for other in list(view_layer.objects):
        if other is None or other is obj:
            continue
        try:
            other.select_set(False)
        except ReferenceError:
            continue
    obj.select_set(True)
    view_layer.objects.active = obj


def _shade_smooth(obj: bpy.types.Object) -> None:
    data = obj.data
    if data is None:
        return
    if obj.type == "MESH":
        if hasattr(data, "shade_smooth"):
            data.shade_smooth()
        else:
            data.polygons.foreach_set("use_smooth", [True] * len(data.polygons))
            data.update()
    elif obj.type == "CURVE":
        for spline in data.splines:
            spline.use_smooth = True


def _shade_flat(obj: bpy.types.Object) -> None:
    data = obj.data
    if data is None:
        return
    if obj.type == "MESH":
        if hasattr(data, "shade_flat"):
            data.shade_flat()
        else:
            data.polygons.foreach_set("use_smooth", [False] * len(data.polygons))
            data.update()
    elif obj.type == "CURVE":
        for spline in data.splines:
            spline.use_smooth = False


def _new_object(context, name: str, data) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    data.name = name
    _link_object(context, obj)
    return obj


def _shorten(text: str, limit: int = 26) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _base_object_name(settings: FunctionGenSettings, source: str = "") -> str:
    """根据模式生成物体名，例如 "函数圆管 sin(x) * 2"。"""
    label = MODE_LABELS.get(settings.mode, "函数图形")
    if settings.mode in PARAM_MODES:
        text = _shorten(source or settings.curve_x, 20) if settings.mode == "PARAM_CURVE" else "参数曲面"
    else:
        text = _shorten(source or settings.formula)
    return f"{label} {text}".strip()


def _validate_range(start: float, end: float, label: str = "范围") -> None:
    if not (math.isfinite(start) and math.isfinite(end)):
        raise GenerationError(f"{label}必须是有限数值")
    if start >= end:
        raise GenerationError(f"{label}起始值必须小于结束值（当前：{start:g} >= {end:g}）")


def _check_resolution(segments: int) -> int:
    if segments < 1:
        raise GenerationError("分辨率（分段数）至少为 1")
    return int(segments)


def effective_segments(settings, span: float, is_surface: bool = False) -> tuple:
    """按"自动加密采样"设置计算实际分段数，返回 (分段数, 是否被加密)。

    规则：effective = max(用户分段数, 用户分段数 × 范围 / 10)，
    仅在 AUTO 开启且范围较大时增加，不会减少用户设置的密度；
    曲线/曲面各有上限，防止范围特别大时卡死。
    """
    user = _check_resolution(settings.resolution)
    if not getattr(settings, "adaptive_sampling", True) or span <= 0:
        return user, False
    cap = _MAX_SURFACE_SEGMENTS if is_surface else _MAX_CURVE_SEGMENTS
    if user >= cap:
        return user, False
    scaled = int(round(user * span / _REFERENCE_SPAN))
    effective = min(cap, max(user, scaled))
    return effective, effective != user


# ---------------------------------------------------------------------------
# 采样（带无定义点容错）
# ---------------------------------------------------------------------------

def _linspace(start: float, end: float, segments: int) -> List[float]:
    step = (end - start) / segments
    return [start + step * i for i in range(segments)] + [end]


def _try_value(func, **values):
    """调用公式求值；成功返回 (True, value)，失败返回 (False, 错误信息)。"""
    try:
        return True, func.evaluate(**values)
    except FormulaError as exc:
        return False, str(exc)


def _sample_curve(
    formulas: Sequence[Formula],
    start: float,
    end: float,
    segments: int,
    strict: bool,
) -> Tuple[List[List[Tuple[float, float, float]]], int]:
    """采样曲线，返回 (多段点列表, 无定义点数量)。

    formulas 只有 1 个时是高度场圆管（点 = (0, t, z)），
    有 3 个时是参数曲线（点 = (x(t), y(t), z(t))）。
    无定义点在 AUTO 模式下把曲线断成多段；STRICT 模式直接抛错。
    """
    values = _linspace(start, end, segments)
    is_param = len(formulas) == 3
    segments_points: List[List[Tuple[float, float, float]]] = []
    current: List[Tuple[float, float, float]] = []
    invalid = 0

    for t in values:
        if is_param:
            ok_x, x = _try_value(formulas[0], t=t)
            ok_y, y = _try_value(formulas[1], t=t)
            ok_z, z = _try_value(formulas[2], t=t)
            ok = ok_x and ok_y and ok_z
            error = next((msg for ok_flag, msg in ((ok_x, x), (ok_y, y), (ok_z, z)) if not ok_flag), "")
        else:
            ok, z = _try_value(formulas[0], x=t, y=t)
            x, y = 0.0, t
            error = "" if ok else str(z)

        if not ok:
            invalid += 1
            if strict:
                raise FormulaError(error)
            # AUTO：结束当前段，跳过该点
            if len(current) >= 2:
                segments_points.append(current)
            current = []
            continue
        current.append((float(x), float(y), float(z)))

    if len(current) >= 2:
        segments_points.append(current)
    elif len(current) == 1:
        # 单点无法成管，忽略
        pass

    if not segments_points:
        if invalid:
            raise GenerationError(
                f"范围内所有采样点都无定义（共 {invalid} 个），请调整范围或检查公式。"
                "例如 1/x 在 x=0 处无定义，可把范围改为 0.1 ~ 5"
            )
        raise GenerationError("没有采样到有效点，请检查范围与分辨率")
    return segments_points, invalid


def _sample_height_surface(
    formula: Formula,
    start: float,
    end: float,
    segments: int,
    strict: bool,
) -> Tuple[List[List[float]], int]:
    """高度场曲面采样：返回按行（y）组织的 z 值二维表，无定义点用邻近值填充。"""
    values = _linspace(start, end, segments)
    grid: List[List[Optional[float]]] = []
    invalid = 0
    for y in values:
        row: List[Optional[float]] = []
        for x in values:
            ok, z = _try_value(formula, x=x, y=y)
            if ok:
                row.append(float(z))
            else:
                invalid += 1
                if strict:
                    raise FormulaError(str(z))
                row.append(None)
        grid.append(row)
    _fill_missing(grid, invalid, "曲面")
    return grid, invalid  # type: ignore[return-value]


def _sample_param_surface(
    formulas: Sequence[Formula],
    u_start: float,
    u_end: float,
    v_start: float,
    v_end: float,
    segments_u: int,
    segments_v: int,
    strict: bool,
) -> Tuple[List[List[Tuple[float, float, float]]], int]:
    """参数曲面采样：返回按 v 行、u 列组织的三维点表，无定义点用邻近值填充。"""
    u_values = _linspace(u_start, u_end, segments_u)
    v_values = _linspace(v_start, v_end, segments_v)
    grid: List[List[Optional[Tuple[float, float, float]]]] = []
    invalid = 0
    for v in v_values:
        row: List[Optional[Tuple[float, float, float]]] = []
        for u in u_values:
            ok_x, x = _try_value(formulas[0], u=u, v=v)
            ok_y, y = _try_value(formulas[1], u=u, v=v)
            ok_z, z = _try_value(formulas[2], u=u, v=v)
            ok = ok_x and ok_y and ok_z
            if ok:
                row.append((float(x), float(y), float(z)))
            else:
                invalid += 1
                if strict:
                    error = next(
                        (msg for flag, msg in ((ok_x, x), (ok_y, y), (ok_z, z)) if not flag),
                        "无定义点",
                    )
                    raise FormulaError(error)
                row.append(None)
        grid.append(row)
    _fill_missing(grid, invalid, "参数曲面")
    return grid, invalid  # type: ignore[return-value]


def _fill_missing(grid: List[List[Optional[object]]], invalid: int, label: str) -> None:
    """把二维表里的 None 用同一行的邻近值填充；整行无效时借用最近的有效行。

    全部无效时抛 GenerationError（提示用户调整范围）。
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    if not rows or not cols:
        raise GenerationError("采样网格为空")

    # 1) 每行：前向 / 后向填充
    for row in grid:
        last = None
        for index, value in enumerate(row):
            if value is None:
                row[index] = last
            else:
                last = value
        last = None
        for index in range(cols - 1, -1, -1):
            if row[index] is None:
                row[index] = last
            else:
                last = row[index]

    # 2) 整行都无效：借用最近的有效行
    valid_rows = [row for row in grid if row and row[0] is not None]
    if not valid_rows:
        raise GenerationError(
            f"{label}范围内所有采样点都无定义（共 {invalid} 个），请调整范围或检查公式。"
            "例如 1/x 在 x=0 处无定义，可把范围改为 0.1 ~ 5"
        )
    fallback = valid_rows[0]
    for index, row in enumerate(grid):
        if not row or row[0] is None:
            grid[index] = list(fallback)


# ---------------------------------------------------------------------------
# 曲线圆管构建（支持多段样条）
# ---------------------------------------------------------------------------

def _weld_curve_caps(mesh: bpy.types.Mesh, distance: float = 1e-6) -> None:
    """焊接端盖与管身重合的顶点，得到真正封闭（水密）的网格。

    Blender 5.x 把曲线 Bevel 的端盖生成为独立的圆片几何（顶点与管身端面重合
    但没有共享顶点），焊接后：边界边 0、单连通，可用于 3D 打印。
    """
    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        if not bm.verts:
            return
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=distance)
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()


def _build_tube_from_segments(
    context,
    settings: FunctionGenSettings,
    segments_points: List[List[Tuple[float, float, float]]],
    name: str,
    effective_count: int = 0,
) -> bpy.types.Object:
    """用多段空间点生成圆管：每段一条 Bezier 样条，Bevel 倒角 + 封口 + 转网格。

    effective_count > 用户分辨率时（自动加密采样），按比例降低 resolution_u：
    路径精度来自加密后的控制点，再细分只会让网格白白变大。
    """
    radius = max(0.0, float(settings.tube_radius))
    user_count = max(1, int(settings.resolution))
    effective_count = max(user_count, int(effective_count or user_count))
    resolution_u = int(round(settings.curve_resolution * user_count / effective_count))
    resolution_u = max(1, min(max(1, int(settings.curve_resolution)), resolution_u))

    curve = bpy.data.curves.new("函数曲线", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = resolution_u
    curve.bevel_depth = radius
    curve.bevel_resolution = max(0, int(settings.bevel_resolution))
    curve.use_fill_caps = bool(getattr(settings, "fill_caps", True))
    curve.twist_mode = "MINIMUM"

    for points in segments_points:
        if len(points) < 2:
            continue
        spline = curve.splines.new("BEZIER")
        spline.bezier_points.add(len(points) - 1)
        for bezier_point, (x, y, z) in zip(spline.bezier_points, points):
            bezier_point.co = (x, y, z)
            bezier_point.handle_left_type = "AUTO"
            bezier_point.handle_right_type = "AUTO"
        spline.use_smooth = True

    curve_obj = _new_object(context, "函数曲线", curve)
    if not settings.convert_to_mesh:
        curve_obj.name = name
        curve_obj.data.name = name
        return curve_obj

    depsgraph = context.evaluated_depsgraph_get()
    evaluated = curve_obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated)
    if mesh is None or len(mesh.vertices) == 0:
        raise GenerationError(
            "曲线转换为网格失败：请检查管道半径是否大于 0（半径为 0 时圆管没有体积）"
        )
    _weld_curve_caps(mesh)
    mesh.name = name

    bpy.data.objects.remove(curve_obj, do_unlink=True)
    bpy.data.curves.remove(curve)
    context.view_layer.update()
    return _new_object(context, name, mesh)


# ---------------------------------------------------------------------------
# 曲面网格构建
# ---------------------------------------------------------------------------

def _build_surface_from_points(
    context,
    settings: FunctionGenSettings,
    rows_points: List[List[Tuple[float, float, float]]],
    name: str,
    weld: bool = False,
) -> bpy.types.Object:
    """把按行组织的三维点表转成网格（四边形面）。"""
    rows = len(rows_points)
    cols = len(rows_points[0]) if rows else 0
    if rows < 2 or cols < 2:
        raise GenerationError("曲面网格太小：请把分辨率调到 2 以上")

    vertices: List[Tuple[float, float, float]] = []
    for row in rows_points:
        vertices.extend(row)

    faces: List[Tuple[int, int, int, int]] = []
    for j in range(rows - 1):
        row_offset = j * cols
        next_row_offset = row_offset + cols
        for i in range(cols - 1):
            faces.append(
                (
                    row_offset + i,
                    row_offset + i + 1,
                    next_row_offset + i + 1,
                    next_row_offset + i,
                )
            )

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.validate(verbose=False)
    mesh.update(calc_edges=True)

    if weld:
        import bmesh

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-6)
            bm.to_mesh(mesh)
            mesh.update()
        finally:
            bm.free()

    return _new_object(context, name, mesh)


# ---------------------------------------------------------------------------
# 4 种模式的构建函数
# ---------------------------------------------------------------------------

def _make_formula(source: str, settings, variables=("x", "y")) -> Formula:
    """构造静态求值用的 Formula，并把面板系数参数 a-d 作为默认值带进去。"""
    return Formula(
        source,
        variables,
        parameters=PARAMETERS,
        parameter_defaults=settings.parameter_values(),
    )


def _build_tube(context, settings, sources, strict) -> Tuple[bpy.types.Object, int]:
    formula = _make_formula(sources[0], settings)
    start = float(settings.range_start)
    end = float(settings.range_end)
    _validate_range(start, end)
    segments, densified = effective_segments(settings, end - start, is_surface=False)
    segments_points, invalid = _sample_curve((formula,), start, end, segments, strict)
    name = _base_object_name(settings, sources[0])
    obj = _build_tube_from_segments(context, settings, segments_points, name, effective_count=segments)
    obj[OBJ_PROP_EFFECTIVE] = segments
    return obj, invalid


def _build_surface(context, settings, sources, strict) -> Tuple[bpy.types.Object, int]:
    formula = _make_formula(sources[0], settings)
    start = float(settings.range_start)
    end = float(settings.range_end)
    _validate_range(start, end)
    segments, densified = effective_segments(settings, end - start, is_surface=True)
    grid, invalid = _sample_height_surface(formula, start, end, segments, strict)

    step = (end - start) / segments
    rows = []
    for j, heights in enumerate(grid):
        y = start + step * j
        row = []
        for i, z in enumerate(heights):
            x = start + step * i
            row.append((x, y, float(z)))
        rows.append(row)

    name = _base_object_name(settings, sources[0])
    mesh = _build_surface_from_points(context, settings, rows, name, weld=False)
    mesh[OBJ_PROP_EFFECTIVE] = segments
    return mesh, invalid


def _build_param_curve(context, settings, sources, strict) -> Tuple[bpy.types.Object, int]:
    formulas = tuple(
        _make_formula(source, settings, PARAM_CURVE_VARIABLES) for source in sources
    )
    start = float(settings.range_start)
    end = float(settings.range_end)
    _validate_range(start, end, "t 范围")
    segments, densified = effective_segments(settings, end - start, is_surface=False)
    segments_points, invalid = _sample_curve(formulas, start, end, segments, strict)
    name = _base_object_name(settings, sources[0])
    obj = _build_tube_from_segments(context, settings, segments_points, name, effective_count=segments)
    obj[OBJ_PROP_EFFECTIVE] = segments
    return obj, invalid


def _build_param_surface(context, settings, sources, strict) -> Tuple[bpy.types.Object, int]:
    formulas = tuple(
        _make_formula(source, settings, PARAM_SURFACE_VARIABLES) for source in sources
    )
    u_start = float(settings.range_start)
    u_end = float(settings.range_end)
    v_start = float(settings.range2_start)
    v_end = float(settings.range2_end)
    _validate_range(u_start, u_end, "U 范围")
    _validate_range(v_start, v_end, "V 范围")
    span = max(u_end - u_start, v_end - v_start)
    segments, densified = effective_segments(settings, span, is_surface=True)
    grid, invalid = _sample_param_surface(
        formulas, u_start, u_end, v_start, v_end, segments, segments, strict
    )
    name = _base_object_name(settings, sources[0])
    obj = _build_surface_from_points(
        context, settings, grid, name, weld=bool(settings.weld_seams)
    )
    obj[OBJ_PROP_EFFECTIVE] = segments
    return obj, invalid


# 模式分发表：新增模式时在这里注册（键与 properties.MODE_ITEMS 的 identifier 一致）
MODE_BUILDERS = {
    "TUBE": _build_tube,
    "SURFACE": _build_surface,
    "PARAM_CURVE": _build_param_curve,
    "PARAM_SURFACE": _build_param_surface,
}


# ---------------------------------------------------------------------------
# 物体参数记录 / 回读
# ---------------------------------------------------------------------------

def _json_formulas(settings: FunctionGenSettings, own_formula: str) -> str:
    """批量场景：每个物体只记录自己的那一条公式（附加列表单独记录在主物体上）。"""
    return json.dumps([own_formula], ensure_ascii=False)


def _save_settings_to_object(
    obj: bpy.types.Object,
    settings: FunctionGenSettings,
    own_formula: str = "",
    invalid_points: int = 0,
) -> None:
    obj[OBJ_PROP_MARKER] = True
    obj[OBJ_PROP_VERSION] = ADDON_VERSION
    for prop_key, attr_name in OBJECT_PROP_KEYS.items():
        value = getattr(settings, attr_name)
        obj[prop_key] = value

    if settings.mode in HEIGHT_MODES:
        own_formula = own_formula or settings.formula
        obj[OBJ_PROP_FORMULA] = own_formula
        obj[OBJ_PROP_EXTRA_FORMULAS] = _json_formulas(settings, own_formula)
    else:
        # 参数模式：三个公式分别存
        obj[OBJ_PROP_FORMULA] = own_formula or settings.curve_x
        obj[OBJ_PROP_CURVE_X] = settings.curve_x
        obj[OBJ_PROP_CURVE_Y] = settings.curve_y
        obj[OBJ_PROP_CURVE_Z] = settings.curve_z
        obj[OBJ_PROP_SURF_X] = settings.surf_x
        obj[OBJ_PROP_SURF_Y] = settings.surf_y
        obj[OBJ_PROP_SURF_Z] = settings.surf_z
    obj[OBJ_PROP_INVALID] = int(invalid_points)
    try:
        obj[OBJ_PROP_COLOR_SIG] = materials.color_signature(settings)
    except Exception:
        pass


def save_settings_to_object(obj, settings, own_formula: str = "", invalid_points: int = 0) -> None:
    """公开别名：几何节点模块也用它记录参数。"""
    _save_settings_to_object(obj, settings, own_formula, invalid_points)


def mark_gn_object(obj, value: bool = True) -> None:
    obj[OBJ_PROP_GN] = bool(value)


def is_gn_object(obj) -> bool:
    if not is_generated_object(obj):
        return False
    try:
        return bool(obj.get(OBJ_PROP_GN))
    except (AttributeError, TypeError):
        return False


def is_generated_object(obj) -> bool:
    if obj is None or getattr(obj, "type", None) not in {"MESH", "CURVE"}:
        return False
    try:
        return bool(obj.get(OBJ_PROP_MARKER))
    except (AttributeError, TypeError):
        return False


def read_settings_from_object(obj: bpy.types.Object, settings: FunctionGenSettings) -> bool:
    """把物体上记录的本插件参数读回参数组。成功返回 True。"""
    if not is_generated_object(obj):
        return False
    for prop_key, attr_name in OBJECT_PROP_KEYS.items():
        if prop_key in obj:
            try:
                setattr(settings, attr_name, obj[prop_key])
            except (TypeError, ValueError):
                pass

    # 批量公式列表：单物体只回读它自己的公式
    settings.extra_formulas.clear()
    if OBJ_PROP_FORMULA in obj:
        settings.formula = obj[OBJ_PROP_FORMULA]

    # 几何节点物体：以修改器里"当前实际值"为准（用户可能在修改器面板改过）
    for name, value in _read_gn_modifier_inputs(obj).items():
        attr = {
            "范围起始": "range_start",
            "范围结束": "range_end",
            "范围2起始": "range2_start",
            "范围2结束": "range2_end",
            "分辨率": "resolution",
            "管道半径": "tube_radius",
        }.get(name)
        if not attr:
            continue
        try:
            setattr(settings, attr, int(round(value)) if attr == "resolution" else float(value))
        except (TypeError, ValueError):
            pass
    return True


def _read_gn_modifier_inputs(obj) -> Dict[str, float]:
    """读取几何节点修改器暴露的输入值（Blender 5.x 的 properties.inputs.<id>.value）。"""
    values: Dict[str, float] = {}
    for modifier in obj.modifiers:
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        for item in modifier.node_group.interface.items_tree:
            if getattr(item, "in_out", None) != "INPUT":
                continue
            identifier = getattr(item, "identifier", "")
            try:
                wrapper = getattr(modifier.properties.inputs, identifier)
                values[item.name] = wrapper.value
            except Exception:
                continue
    return values


# ---------------------------------------------------------------------------
# 生成入口
# ---------------------------------------------------------------------------

def _apply_material_safe(obj: bpy.types.Object, settings: FunctionGenSettings) -> None:
    """给新物体准备材质：优先面板指定材质，其次保留已有材质，最后自动创建。

    材质只是普通 Blender 材质，用户可以在 Shader Editor 里随意编辑；
    这里不会覆盖已有材质槽里的材质。
    """
    try:
        materials.ensure_object_material(obj, settings)
    except Exception:
        traceback.print_exc()


def _finish_object(
    context,
    obj: bpy.types.Object,
    settings: FunctionGenSettings,
    own_formula: str,
    invalid: int,
) -> bpy.types.Object:
    if settings.smooth_shading:
        _shade_smooth(obj)
    else:
        _shade_flat(obj)
    _save_settings_to_object(obj, settings, own_formula, invalid)
    _apply_material_safe(obj, settings)
    obj.location = _cursor_location(context) if settings.at_cursor else Vector((0.0, 0.0, 0.0))
    _select_only(context, obj)
    return obj


def _sources_for_mode(settings: FunctionGenSettings) -> List[Tuple[str, List[str]]]:
    """返回本模式要生成的所有物体：(物体名用公式, 公式列表)。"""
    if settings.mode in HEIGHT_MODES:
        return [(source, [source]) for source in settings.height_formulas()]
    if settings.mode == "PARAM_CURVE":
        return [(settings.curve_x, [settings.curve_x, settings.curve_y, settings.curve_z])]
    if settings.mode == "PARAM_SURFACE":
        return [(settings.surf_x, [settings.surf_x, settings.surf_y, settings.surf_z])]
    raise GenerationError(f"未知的生成模式：{settings.mode}")


def generate_single(
    context,
    settings: FunctionGenSettings,
    source: Optional[str] = None,
) -> Tuple[bpy.types.Object, int]:
    """按当前模式生成一个物体，返回 (物体, 无定义点数量)。"""
    builder = MODE_BUILDERS.get(settings.mode)
    if builder is None:
        raise GenerationError(f"未知的生成模式：{settings.mode}")

    strict = settings.singular_handling == "STRICT"
    if settings.mode in HEIGHT_MODES:
        sources = [source if source is not None else settings.formula]
    elif settings.mode == "PARAM_CURVE":
        sources = [settings.curve_x, settings.curve_y, settings.curve_z]
    else:
        sources = [settings.surf_x, settings.surf_y, settings.surf_z]

    obj, invalid = builder(context, settings, sources, strict)
    own_formula = sources[0]
    return _finish_object(context, obj, settings, own_formula, invalid), invalid


def generate_batch(context, settings: FunctionGenSettings) -> List[Tuple[bpy.types.Object, int]]:
    """按当前模式生成（批量公式模式下会生成多个物体），返回 [(物体, 无定义点数)]。"""
    results: List[Tuple[bpy.types.Object, int]] = []
    for _name_source, sources in _sources_for_mode(settings):
        if settings.mode in HEIGHT_MODES:
            obj, invalid = generate_single(context, settings, source=sources[0])
        else:
            builder = MODE_BUILDERS[settings.mode]
            strict = settings.singular_handling == "STRICT"
            obj, invalid = builder(context, settings, sources, strict)
            obj = _finish_object(context, obj, settings, sources[0], invalid)
        results.append((obj, invalid))
    # 批量时最后一个物体保持选中；但把其它物体取消选择（_finish_object 已保证）
    return results


def generate(context, settings: FunctionGenSettings):
    """兼容旧接口：生成并返回第一个物体。"""
    results = generate_batch(context, settings)
    return results[0][0] if results else None


# 公开别名（几何节点模块会用到）
select_only = _select_only
cursor_location = _cursor_location