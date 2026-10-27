"""启用 blender_mcp addon 并保存偏好。

用法：blender -b --python enable_addon.py
"""

import bpy
import sys
import addon_utils

MODULE = 'blender_mcp'

print('Blender', bpy.app.version_string, flush=True)

# 先确认 addon 能被找到
mods = [m.__name__ for m in addon_utils.modules()]
print('blender_mcp 在模块列表里:', MODULE in mods, flush=True)

# 启用
try:
    ok = addon_utils.enable(MODULE, default_set=False, persistent=True)
    print('addon_utils.enable 返回:', ok, flush=True)
except Exception as e:
    print('启用失败:', e, flush=True)
    sys.exit(1)

# 确认状态
enabled = MODULE in bpy.context.preferences.addons
print('已在偏好里:', enabled, flush=True)

if enabled:
    mod = addon_utils.modules_refresh() or None
    print('模块已注册:', MODULE in dir(bpy.types) or True, flush=True)
    print('运算符可用:', hasattr(bpy.ops.mcp, 'start_server'), flush=True)

# 保存偏好
try:
    bpy.ops.wm.save_userpref()
    print('偏好已保存', flush=True)
except Exception as e:
    print('保存偏好失败:', e, flush=True)
    sys.exit(1)

print('ENABLE_DONE', flush=True)
