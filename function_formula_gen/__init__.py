# SPDX-License-Identifier: GPL-3.0-or-later
# 函数公式生成器 / Function Formula Generator
"""函数公式生成器（Function Formula Generator）—— Blender 5.2+ 插件入口。

插件功能
========
* 在 3D 视图侧栏输入数学公式，按范围与分辨率生成：
    - 曲线圆管（TUBE）  ：沿 Y 轴的 Bezier 路径 + 曲线 Bevel 倒角，带封口，可转网格；
    - 曲面网格（SURFACE）：XY 平面网格 + 公式高度，平滑着色；
    - 参数曲线（PARAM_CURVE）：x(t), y(t), z(t) 任意空间曲线管；
    - 参数曲面（PARAM_SURFACE）：x(u,v), y(u,v), z(u,v) 真三维曲面（球面/环面等）。
* 自动彩色渐变材质、一键渲染预览（自动显示在图片编辑器）、批量公式；
* Desmos 风格输入（数学键盘 + 2x、x^2、π、√、|x|、y= 等写法）；几何节点参数实时同步；
* 几何节点功能扩展：范围/自适应/平滑/缩放/偏移/截面边数/封口/接缝/属性全外露，
  公式系数 a-d 作为节点输入（可连噪声/场景时间），新增函数场节点（值场/向量场）；
* 自定义颜色（v1.4）：色带可选「自定义颜色」，可增删色标、改位置/任意颜色；单色模式可自选颜色；
* 材质统一（v1.5）：静态/几何节点物体都用普通 Blender 材质，在 Shader Editor 里编辑；
  几何节点组新增「材质」输入 + Set Material；面板只保留新建/套色/应用/打开着色器快捷键；
* 大范围自动加密采样，避免曲线抖动；1/x 等无定义点自动容错。
* 在几何节点编辑器里也提供"函数公式节点"（Add 菜单），公式会被编译成几何节点，
  可在修改器里继续调范围 / 分辨率 / 半径。
* 生成后自动 Shade Smooth、自动选中新物体，并把参数记录在物体上，
  支持"读取参数 / 更新选中物体"。

模块结构（便于维护）
====================
    __init__.py   插件信息 bl_info、register / unregister 总入口
    formula.py    公式 AST 白名单校验 + 受限 eval + 采样（纯 Python，可单独测试）
    properties.py 所有面板参数（Scene.function_gen）与示例公式
    generators.py 曲线圆管 / 曲面网格的几何生成（静态模型）
    operators.py  生成 / 校验 / 读取参数 / 更新物体 等操作符
    gn_nodes.py   几何节点集成：公式编译成节点组、Add 菜单入口、插入节点
    materials.py  自动渐变材质（色带 / 着色依据）
    preview.py    一键渲染预览（自动相机 + 三点灯光 + 渲染）
    ui.py         3D 视图侧栏面板 + 几何节点编辑器侧栏面板

维护提示
========
* 加功能优先加在对应模块里：几何相关 -> generators，节点相关 -> gn_nodes，界面 -> ui。
* 各模块都提供 register()/unregister()，在本文件 _MODULES 里登记即可（顺序有关：
  先参数、再操作符、再几何节点菜单、最后界面）。
* 更新版本号时同步 __init__ 的 bl_info["version"]。
"""
from __future__ import annotations

bl_info = {
    "name": "函数公式生成器 (Function Formula Generator)",
    "author": "DSH Blender Addon",
    "version": (1, 5, 0),
    "blender": (5, 2, 0),
    "location": "3D 视图侧栏(N) > 函数生成；几何节点编辑器 Add 菜单 > 函数公式节点",
    "description": "用数学公式生成 3D 函数图像：高度场曲面/圆管、参数曲线/参数曲面，带自动彩色材质、一键渲染预览、批量公式与几何节点版本",
    "warning": "",
    "doc_url": "",
    "category": "Add Mesh",
}

# 模块注册顺序：参数 -> 操作符 -> 几何节点(菜单/操作符) -> 界面
_MODULES = None


def _get_modules():
    """延迟导入，避免 Blender 在扫描插件时 import 失败导致整个包不可用。"""
    global _MODULES
    if _MODULES is None:
        from . import properties, operators, gn_nodes, ui

        _MODULES = (properties, operators, gn_nodes, ui)
    return _MODULES


def register() -> None:
    for module in _get_modules():
        module.register()


def unregister() -> None:
    for module in reversed(_get_modules()):
        try:
            module.unregister()
        except Exception:
            # 卸载时单个模块失败不应阻塞其它模块卸载（例如用户热重载插件）
            import traceback

            traceback.print_exc()


if __name__ == "__main__":  # 允许在 Blender 文本编辑器里直接运行本文件做调试
    register()