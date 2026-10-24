# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""材质编辑器辅助（v1.5）：把 Blender 原生 Shader Editor 打开到指定材质。

材质统一由 Blender 材质系统管理；插件只负责"把材质放到物体上/节点组材质输入里"，
并提供打开 Shader Editor 的快捷入口。
"""
from __future__ import annotations

import bpy


def _shader_space_in_area(area):
    space = getattr(area, "spaces", None)
    if not space:
        return None
    try:
        active = area.spaces.active
    except Exception:
        return None
    if getattr(active, "type", "") == "NODE_EDITOR" and getattr(active, "tree_type", "") == "ShaderNodeTree":
        return active
    return None


def _show_material_in_space(space, material) -> bool:
    node_tree = getattr(material, "node_tree", None)
    if node_tree is None:
        return False
    try:
        space.pin = True
    except Exception:
        pass
    try:
        space.node_tree = node_tree
        return True
    except Exception:
        pass
    try:
        space.id = node_tree
        return True
    except Exception:
        return False


def open_shader_editor(material) -> bool:
    """打开/新建一个 Shader Editor 并显示指定材质；返回是否成功（GUI 下）。"""
    if material is None or material.node_tree is None or bpy.app.background:
        return False
    # 1) 优先复用已有 Shader Editor 区域
    for window in bpy.context.window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            space = _shader_space_in_area(area)
            if space is not None and _show_material_in_space(space, material):
                return True
    # 2) 没有就新开一个窗口，放成 Shader Editor
    try:
        bpy.ops.wm.window_new()
        window = bpy.context.window_manager.windows[-1]
        screen = window.screen
        if screen is None or len(screen.areas) == 0:
            return False
        area = max(screen.areas, key=lambda item: item.width * item.height)
        area.type = "NODE_EDITOR"
        space = area.spaces.active
        try:
            space.tree_type = "ShaderNodeTree"
        except Exception:
            pass
        return _show_material_in_space(space, material)
    except Exception:
        import traceback

        traceback.print_exc()
        return False
