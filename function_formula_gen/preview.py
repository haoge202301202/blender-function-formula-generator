# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""一键渲染预览模块（v1.1 新增）。

点一下"渲染预览"就能得到一张自动取景、自动打光的图片：
    1. 创建/复用「函数预览相机」+ 三点灯光（放在独立集合「函数图像·预览」里）；
    2. 根据选中物体的真实包围盒自动取景（斜 45° 俯视）；
    3. 按面板参数临时设置渲染引擎/分辨率/采样并渲染；
    4. 渲染完把场景的引擎/相机/世界/渲染设置**恢复原样**（不在场景里留下改动）；
    5. 把图片加载进 Blender 并在图片编辑器里显示（没有图片编辑器时只保存文件）。

维护提示
========
* 灯光/相机都按固定名字复用，不会越渲染越多；
* 改取景角度：修 _frame_camera() 里的 direction；改灯光风格：修 _place_lights()；
* 预览渲染的引擎枚举在 properties.PREVIEW_ENGINE_ITEMS，新增引擎要同步 _engine_identifier()。
"""
from __future__ import annotations

import math
import os
import traceback
from typing import Optional, Tuple

import bpy
from mathutils import Vector

PREVIEW_COLLECTION = "函数图像·预览"
CAMERA_NAME = "函数预览相机"
LIGHT_NAMES = ("函数预览主光", "函数预览补光", "函数预览轮廓光")
WORLD_NAME = "函数预览世界"


class PreviewError(ValueError):
    """预览渲染失败（例如没有选中可渲染的物体）。"""


# ---------------------------------------------------------------------------
# 场景准备（相机 / 灯光 / 世界）
# ---------------------------------------------------------------------------

def _ensure_collection(scene) -> bpy.types.Collection:
    collection = bpy.data.collections.get(PREVIEW_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(PREVIEW_COLLECTION)
        scene.collection.children.link(collection)
    # 相机/灯光只在渲染时用，视口里隐藏，避免一堆线框挡住模型（不影响渲染）
    try:
        collection.hide_viewport = True
    except Exception:
        pass
    return collection


def _ensure_object(collection, name: str, data) -> bpy.types.Object:
    obj = bpy.data.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, data)
        collection.objects.link(obj)
    elif obj.name not in collection.objects:
        collection.objects.link(obj)
    return obj


def ensure_camera_rig(scene):
    """创建/复用预览相机与三点灯光，返回 (相机物体, 灯光物体列表)。"""
    collection = _ensure_collection(scene)
    camera_data = bpy.data.cameras.get("函数预览相机数据") or bpy.data.cameras.new("函数预览相机数据")
    camera_data.lens = 50.0
    camera_data.sensor_width = 36.0
    camera = _ensure_object(collection, CAMERA_NAME, camera_data)

    lights = []
    for index, name in enumerate(LIGHT_NAMES):
        light_data = bpy.data.lights.get(name + "数据")
        if light_data is None:
            light_data = bpy.data.lights.new(name + "数据", type="AREA")
        light_data.type = "AREA"
        light_data.shape = "SQUARE"
        light_data.size = 2.0
        light_obj = _ensure_object(collection, name, light_data)
        lights.append(light_obj)
    return camera, lights


def _ensure_world() -> bpy.types.World:
    world = bpy.data.worlds.get(WORLD_NAME)
    if world is None:
        world = bpy.data.worlds.new(WORLD_NAME)
    node_tree = world.node_tree
    if node_tree is None:
        world.use_nodes = True
        node_tree = world.node_tree
    background = node_tree.nodes.get("Background")
    if background is None:
        background = node_tree.nodes.new("ShaderNodeBackground")
        output = node_tree.nodes.get("World Output") or node_tree.nodes.new("ShaderNodeOutputWorld")
        node_tree.links.new(background.outputs[0], output.inputs[0])
    background.inputs[0].default_value = (0.012, 0.015, 0.022, 1.0)
    background.inputs[1].default_value = 1.0
    return world


# ---------------------------------------------------------------------------
# 取景与灯光
# ---------------------------------------------------------------------------

def _evaluated_bounds(obj: bpy.types.Object) -> Tuple[Vector, float]:
    """返回物体（含几何节点求值后）的世界空间包围盒中心与半径。"""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except Exception as exc:
        raise PreviewError(f"无法读取物体几何：{exc}") from None
    try:
        if mesh is None or len(mesh.vertices) == 0:
            raise PreviewError("选中物体没有几何，无法预览（请先生成函数图形）")
        matrix = obj.matrix_world
        corners = [matrix @ vertex.co for vertex in mesh.vertices]
    finally:
        evaluated.to_mesh_clear()

    low = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    high = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    center = (low + high) * 0.5
    radius = max((corner - center).length for corner in corners)
    return center, max(radius, 0.05)


def _frame_camera(camera: bpy.types.Object, center: Vector, radius: float) -> None:
    """按包围盒自动取景（斜 45° 俯视）。"""
    direction = Vector((1.0, -1.0, 0.75)).normalized()
    fov = 2.0 * math.atan(camera.data.sensor_width * 0.5 / camera.data.lens)
    distance = radius / math.tan(fov * 0.5) * 1.45
    camera.location = center + direction * distance
    track = (center - camera.location).to_track_quat("-Z", "Y")
    camera.rotation_euler = track.to_euler()


def _place_lights(lights, center: Vector, radius: float) -> None:
    """三点灯光：主光 / 补光 / 轮廓光，位置与功率随物体大小缩放。"""
    if len(lights) < 3:
        return
    distance = radius * 3.0 + 2.0
    energy = 250.0 * (radius / 3.0) ** 2 + 80.0
    setups = (
        (Vector((1.2, -1.6, 1.8)).normalized() * distance, energy, radius * 1.2 + 0.5),
        (Vector((-1.8, -0.6, 0.6)).normalized() * distance, energy * 0.35, radius * 1.5 + 0.5),
        (Vector((0.2, 1.8, 1.2)).normalized() * distance, energy * 0.5, radius * 1.0 + 0.5),
    )
    for light_obj, (offset, light_energy, size) in zip(lights, setups):
        light_obj.location = center + offset
        track = (center - light_obj.location).to_track_quat("-Z", "Y")
        light_obj.rotation_euler = track.to_euler()
        light_obj.data.energy = max(20.0, light_energy)
        light_obj.data.size = max(0.2, size)


# ---------------------------------------------------------------------------
# 渲染设置
# ---------------------------------------------------------------------------

def _engine_identifier(scene, wanted: str) -> str:
    if wanted == "FOLLOW":
        return scene.render.engine
    if wanted == "CYCLES":
        return "CYCLES"
    for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items:
        if "EEVEE" in item.identifier:
            return item.identifier
    return scene.render.engine


def _parse_resolution(text: str) -> Tuple[int, int]:
    try:
        width, height = text.lower().split("x")
        return max(16, int(width)), max(16, int(height))
    except (ValueError, AttributeError):
        return 960, 720


def _set_engine_settings(scene, engine: str, samples: int) -> None:
    scene.render.engine = engine
    if engine == "CYCLES":
        scene.cycles.samples = max(1, int(samples))
        try:
            scene.cycles.device = "CPU"
        except AttributeError:
            pass
    elif "EEVEE" in engine:
        try:
            scene.eevee.taa_render_samples = max(1, int(samples))
        except AttributeError:
            pass


def _show_image(context, image: bpy.types.Image) -> Optional[bpy.types.Area]:
    """如果当前窗口/其它窗口里有图片编辑器，就把渲染结果显示出来。"""
    windows = getattr(bpy.context.window_manager, "windows", []) if bpy.context.window_manager else []
    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "IMAGE_EDITOR":
                try:
                    area.spaces.active.image = image
                    window.make_active() if hasattr(window, "make_active") else None
                    return area
                except (AttributeError, TypeError):
                    continue
    screen = getattr(context, "screen", None)
    if screen is not None:
        for area in screen.areas:
            if area.type == "IMAGE_EDITOR":
                try:
                    area.spaces.active.image = image
                    return area
                except (AttributeError, TypeError):
                    continue
    return None


def _show_in_new_window(image: bpy.types.Image) -> bool:
    """没有图片编辑器时，新开一个窗口显示预览图（类似 F11 的渲染窗口）。"""
    if bpy.app.background:
        return False
    try:
        bpy.ops.wm.window_new()
        window = bpy.context.window_manager.windows[-1]
        screen = window.screen
        if screen is None or len(screen.areas) == 0:
            return False
        area = max(screen.areas, key=lambda item: item.width * item.height)
        area.type = "IMAGE_EDITOR"
        space = area.spaces.active
        space.image = image
        try:
            space.image_user.use_auto_refresh = True
        except AttributeError:
            pass
        return True
    except Exception:
        traceback.print_exc()
        return False


def _prepare_target(context) -> bpy.types.Object:
    obj = context.active_object
    if obj is None or obj.type not in {"MESH", "CURVE"}:
        raise PreviewError("请先生成函数图形并选中它，再点「渲染预览」")
    return obj


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def render_preview(context, settings) -> dict:
    """渲染当前选中物体的预览图，返回 dict（路径、引擎、分辨率、是否降级）。"""
    scene = context.scene
    target = _prepare_target(context)
    camera, lights = ensure_camera_rig(scene)
    world = _ensure_world()

    center, radius = _evaluated_bounds(target)
    _frame_camera(camera, center, radius)
    _place_lights(lights, center, radius)

    # 记录要临时修改的设置，渲染后恢复
    saved = {
        "engine": scene.render.engine,
        "camera": scene.camera,
        "world": scene.world,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "resolution_percentage": scene.render.resolution_percentage,
        "filepath": scene.render.filepath,
        "film_transparent": scene.render.film_transparent,
        "format": scene.render.image_settings.file_format,
    }
    width, height = _parse_resolution(getattr(settings, "preview_resolution", "960x720"))
    engine = _engine_identifier(scene, getattr(settings, "preview_engine", "FOLLOW"))
    samples = int(getattr(settings, "preview_samples", 32))
    output_path = os.path.join(bpy.app.tempdir, "functiongen_preview.png")
    fallback = False

    try:
        scene.camera = camera
        scene.world = world
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = False
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = output_path
        _set_engine_settings(scene, engine, samples)
        try:
            bpy.ops.render.render(write_still=True)
        except Exception:
            # EEVEE 在无 GPU 的 headless 环境可能失败：自动降级到 Cycles CPU 再试一次
            traceback.print_exc()
            if "CYCLES" not in scene.render.engine:
                engine = "CYCLES"
                _set_engine_settings(scene, engine, max(samples, 16))
                bpy.ops.render.render(write_still=True)
                fallback = True
            else:
                raise
    finally:
        scene.render.engine = saved["engine"]
        scene.camera = saved["camera"]
        scene.world = saved["world"]
        scene.render.resolution_x = saved["resolution_x"]
        scene.render.resolution_y = saved["resolution_y"]
        scene.render.resolution_percentage = saved["resolution_percentage"]
        scene.render.filepath = saved["filepath"]
        scene.render.film_transparent = saved["film_transparent"]
        scene.render.image_settings.file_format = saved["format"]

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        raise PreviewError("渲染没有产生有效图片，请检查渲染引擎设置")

    displayed = False
    try:
        image = _load_preview_image(output_path)
        displayed = _show_image(context, image) is not None
    except Exception:
        traceback.print_exc()
    if not displayed:
        # 没有图片编辑器时：新开一个图片编辑器窗口，或退回 Blender 的 F11 显示
        displayed = _show_in_new_window(image)
        if not displayed:
            try:
                result = bpy.ops.render.view_show()
                displayed = "FINISHED" in result
            except Exception:
                pass

    return {
        "path": output_path,
        "engine": engine,
        "resolution": (width, height),
        "samples": samples,
        "fallback": fallback,
        "displayed": displayed,
        "object": target.name,
    }


def _load_preview_image(path: str) -> bpy.types.Image:
    image = bpy.data.images.load(path, check_existing=True)
    try:
        image.reload()
    except Exception:
        pass
    return image


def open_preview_image(context, path: str) -> bool:
    """在图片编辑器里打开指定预览图；没有图片编辑器时尝试用 F11 渲染结果窗口。

    返回是否成功显示（False 表示只拿到文件路径）。
    """
    if not path or not os.path.exists(path):
        raise PreviewError(f"预览图不存在：{path}")
    image = _load_preview_image(path)
    if _show_image(context, image) is not None:
        return True
    if _show_in_new_window(image):
        return True
    try:
        result = bpy.ops.render.view_show()
        return "FINISHED" in result
    except Exception:
        return False