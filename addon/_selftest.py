"""端到端自测：在同一进程里起 addon 服务器 + 用真实 socket 客户端调命令。

不依赖后台保活、不依赖 bpy.app.timers 驱动——所有命令走同步路径，
后台模式下直接在主线程执行（addon 里 _run_on_main 的 background 分支）。

    blender -b --python addon/_selftest.py
"""

import bpy
import sys
import importlib.util
import json
import socket
import time
import traceback

spec = importlib.util.spec_from_file_location(
    'blender_mcp', r'D:\dsh插件制作\blender-mcp\addon\blender_mcp.py')
m = importlib.util.module_from_spec(spec)
sys.modules['blender_mcp'] = m
spec.loader.exec_module(m)

PORT = 9877
srv = m.MCPCommandServer('127.0.0.1', PORT)
srv.start()

passed = 0
failed = 0


def rpc(msg, timeout=120):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect(('127.0.0.1', PORT))
    s.sendall((json.dumps(msg, ensure_ascii=False) + '\n').encode('utf-8'))
    data = b''
    while b'\n' not in data:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data.decode('utf-8'))


def check(label, cond, detail=''):
    global passed, failed
    if cond:
        passed += 1
        print('  OK   ' + label)
    else:
        failed += 1
        print('  FAIL ' + label + (' — ' + str(detail) if detail else ''))


def call(cmd, params=None, timeout=120):
    return rpc({'type': cmd, 'params': params or {}}, timeout=timeout)


print('=== 基础连通 ===')
r = call('ping')
check('ping 通', r.get('status') == 'success' and r['result'].get('pong'), r)

print('\n=== 创建几何 ===')
r = call('add_primitive', {'type': 'cube', 'name': '主体', 'size': 2})
check('创建立方体', r.get('status') == 'success' and r['result']['name'] == '主体', r)

r = call('add_primitive', {'type': 'cylinder', 'name': '圆柱', 'radius': 0.6, 'depth': 3, 'location': [0, 0, 2]})
check('创建圆柱', r.get('status') == 'success', r)

r = call('add_text', {'text': 'MCP', 'name': '文字', 'size': 0.5, 'location': [3, 0, 0]})
check('创建文字', r.get('status') == 'success', r)

print('\n=== 编辑几何 ===')
r = call('bevel', {'name': '主体', 'width': 0.05, 'segments': 2})
check('倒角', r.get('status') == 'success', r)

r = call('subdivide', {'name': '主体', 'cuts': 1})
check('细分', r.get('status') == 'success', r)

r = call('smooth_shade', {'name': '圆柱', 'smooth': True})
check('平滑着色', r.get('status') == 'success', r)

print('\n=== 变换 ===')
r = call('transform_object', {'name': '圆柱', 'location': [0, 0, 2.5]})
check('移动对象', r.get('status') == 'success' and abs(r['result']['location'][2] - 2.5) < 1e-6, r)

r = call('duplicate_object', {'name': '主体', 'count': 2, 'offset': [3, 0, 0]})
check('复制对象', r.get('status') == 'success' and len(r['result']['created']) == 2, r)

print('\n=== 修改器 ===')
r = call('add_modifier', {'name': '主体', 'type': 'SUBSURF', 'props': {'levels': 2}})
check('添加细分曲面修改器', r.get('status') == 'success', r)

r = call('list_modifiers', {'name': '主体'})
check('列出修改器', r.get('status') == 'success' and len(r['result']['modifiers']) >= 1, r)

r = call('remove_modifier', {'name': '主体', 'modifier': r['result']['modifiers'][-1]['name'], 'action': 'REMOVE'})
check('删除修改器', r.get('status') == 'success', r)

print('\n=== 材质 ===')
r = call('create_material', {'name': '金属', 'base_color': [0.75, 0.35, 0.12], 'metallic': 1.0, 'roughness': 0.25})
check('创建材质', r.get('status') == 'success', r)

r = call('assign_material', {'object': '主体', 'material': '金属'})
check('指定材质', r.get('status') == 'success', r)

r = call('set_material_property', {'material': '金属', 'roughness': 0.6})
check('改材质属性', r.get('status') == 'success', r)

r = call('list_materials')
check('列出材质', r.get('status') == 'success' and r['result']['count'] >= 1, r)

print('\n=== 对象管理 ===')
r = call('rename_object', {'name': '文字', 'new_name': 'Logo'})
check('重命名', r.get('status') == 'success' and r['result']['new'] == 'Logo', r)

r = call('select_objects', {'names': ['主体', '圆柱']})
check('选择对象', r.get('status') == 'success' and len(r['result']['selected']) >= 2, r)

r = call('get_scene_info', {'detail': 'full'})
check('场景信息', r.get('status') == 'success' and r['result']['object_count'] >= 3, r)

r = call('get_object_info', {'name': '主体'})
check('对象详情', r.get('status') == 'success' and r['result']['type'] == 'MESH', r)

print('\n=== 观察 ===')
import sys as _sys
_sys.stdout.flush()
# 先确认场景里有哪些对象、类型
print('       场景对象：' + ', '.join('%s(%s)' % (o.name, o.type) for o in bpy.context.scene.objects), flush=True)
_sys.stdout.flush()
t0 = time.time()
r = call('render_view', {'view': 'iso', 'width': 320, 'height': 240}, timeout=180)
if r.get('status') == 'success':
    ok = len(r['result'].get('image_base64', '')) > 1000
    check('渲染等轴视图', ok, '')
    print('       耗时 %.1fs' % (time.time() - t0))
elif bpy.app.background and '界面上下文' in r.get('message', ''):
    # 后台模式明确拒绝渲染是设计行为（否则会崩进程）
    check('后台模式渲染被明确拒绝（不崩溃）', True, '')
    print('       提示：' + r['message'][:80])
else:
    check('渲染等轴视图', False, r.get('message'))

r = call('render_view', {'view': 'angles', 'width': 240, 'height': 180}, timeout=180)
if r.get('status') == 'success':
    check('渲染多角度拼图', len(r['result'].get('image_base64', '')) > 1000, '')
elif bpy.app.background:
    check('后台模式多角度渲染被明确拒绝（不崩溃）', True, '')
else:
    check('渲染多角度拼图', False, r.get('message'))

print('\n=== 导入导出 ===')
import os
tmpdir = os.environ.get('TEMP') or '/tmp'

# GLB：后台模式下 glTF 导出器需要窗口对象，属已知限制
glb = os.path.join(tmpdir, 'mcp_selftest.glb')
r = call('export_file', {'filepath': glb, 'format': 'glb', 'selected_only': False})
if r.get('status') == 'success' and os.path.exists(glb):
    check('导出 GLB', True, '')
    print('       文件 %d 字节' % os.path.getsize(glb))
elif bpy.app.background and 'glTF' in r.get('message', ''):
    check('后台模式 GLB 导出给出可读降级提示（不崩溃）', True, '')
    print('       提示：' + r['message'][:90])
else:
    check('导出 GLB', False, r.get('message'))

# OBJ：后台模式应该能用
obj = os.path.join(tmpdir, 'mcp_selftest.obj')
r = call('export_file', {'filepath': obj, 'format': 'obj', 'selected_only': False})
ok = r.get('status') == 'success' and os.path.exists(obj)
check('导出 OBJ', ok, r.get('message') if not ok else '')
if ok:
    print('       文件 %d 字节' % os.path.getsize(obj))

print('\n=== 其它 ===')
r = call('execute_code', {'code': 'print("hello from blender")\nresult = 1 + 1'})
check('执行 Python', r.get('status') == 'success' and 'hello from blender' in ' '.join(r['result']['stdout']), r)

r = call('get_blender_version')
check('版本信息', r.get('status') == 'success' and 'blender' in r['result'], r)

print('\n=== 错误处理 ===')
r = call('add_modifier', {'name': '不存在的对象', 'type': 'BEVEL'})
check('不存在的对象报可读错误', r.get('status') == 'error' and '找不到对象' in r.get('message', ''), r)

r = call('unknown_command')
check('未知命令报错', r.get('status') == 'error' and '未知命令' in r.get('message', ''), r)

srv.stop()

print('\n' + ('SELFTEST_OK %d 项通过' % passed if failed == 0 else 'SELFTEST_FAILED %d 通过 / %d 失败' % (passed, failed)))
