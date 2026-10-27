# -*- coding: utf-8 -*-
"""
Blender MCP Addon
=================

在 Blender 里开一个 TCP socket 服务器，接收 MCP 服务器转发来的 JSON 命令并执行。

安装：
    Edit → Preferences → Add-ons → Install… → 选这个文件 → 勾选启用
使用：
    3D 视口按 N → 「Blender MCP」标签 → Start MCP Server

协议：一行一个 JSON。
    请求  {"type":"<命令>", "params":{...}}
    响应  {"status":"success","result":{...}}
          {"status":"error","message":"..."}
"""

import bpy
import bmesh
import json
import os
import socket
import threading
import base64
import math
import tempfile
import traceback
import time

bl_info = {
    'name': 'Blender MCP',
    'author': 'local',
    'version': (1, 2, 3),
    'blender': (3, 0, 0),
    'location': 'View3D > Sidebar > Blender MCP',
    'description': '供 MCP 服务器调用的建模命令接口',
    'category': 'Interface',
}

HOST = '127.0.0.1'
DEFAULT_PORT = 9876


# ══════════════════════════════════════════════════════ 工具函数 ══

def _obj(name):
    """按名字取对象，取不到抛可读错误。"""
    o = bpy.data.objects.get(name)
    if o is None:
        raise ValueError('找不到对象「%s」。用 get_scene_info 查现有对象名。' % name)
    return o


def _active():
    """安全取活动对象。

    `blender -b` 模式下 `_active()` 可能不存在（上下文不完整），
    所以统一走这个函数，取不到就返回 None。
    """
    try:
        return bpy.context.view_layer.objects.active
    except AttributeError:
        return None


def _active_or_selected():
    """活动对象，没有就取第一个选中的。"""
    a = _active()
    if a is not None:
        return a
    for o in bpy.context.scene.objects:
        try:
            if o.select_get():
                return o
        except Exception:
            pass
    return None


def _deselect_all():
    for o in bpy.context.scene.objects:
        o.select_set(False)


def _activate(obj):
    """让对象成为唯一选中且活动。"""
    _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _mode(mode):
    """安全切换编辑模式。

    后台模式下 _active_or_selected() 可能不存在，用 view_layer 兜底。
    """
    cur = _active_or_selected()
    if cur is None:
        return
    try:
        if cur.mode == mode:
            return
    except AttributeError:
        return
    try:
        _ops(bpy.ops.object.mode_set, mode=mode)
    except Exception:
        pass


def _ops(call, **kwargs):
    """执行 bpy.ops，自动补一个可用的上下文覆盖。

    为什么需要：`blender -b` 没有窗口和区域，很多 operator 的 poll() 会失败
    （典型报错 "context is incorrect"）。用 `bpy.context.temp_override`
    补上 window/screen/area/region 之后，绝大多数 operator 就能正常工作。
    GUI 模式下这些字段本来就存在，覆盖是幂等的。
    """
    ctx = bpy.context
    overrides = {}

    # 找可用的 window / screen / area
    window = getattr(ctx, 'window', None)
    screen = getattr(ctx, 'screen', None)
    area = getattr(ctx, 'area', None)
    region = getattr(ctx, 'region', None)

    if window is None:
        try:
            wm = bpy.context.window_manager
            if wm.windows:
                window = wm.windows[0]
        except Exception:
            pass
    if screen is None and window is not None:
        try:
            screen = window.screen
        except Exception:
            pass
    if area is None and screen is not None:
        try:
            for a in screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break
            if area is None and len(screen.areas):
                area = screen.areas[0]
        except Exception:
            pass
    if region is None and area is not None:
        try:
            for r in area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break
            if region is None and len(area.regions):
                region = area.regions[0]
        except Exception:
            pass

    if window is not None:
        overrides['window'] = window
    if screen is not None:
        overrides['screen'] = screen
    if area is not None:
        overrides['area'] = area
    if region is not None:
        overrides['region'] = region

    # 保证有活动对象，否则 object.* 类 operator 的 poll 会失败
    act = _active_or_selected()
    if act is not None:
        overrides['object'] = act
        overrides['active_object'] = act
        try:
            if act.mode != 'OBJECT':
                overrides['selected_objects'] = [act]
                overrides['selected_editable_objects'] = [act]
        except Exception:
            pass

    if not overrides:
        return call(**kwargs)

    try:
        with ctx.temp_override(**overrides):
            return call(**kwargs)
    except (AttributeError, TypeError):
        # 老版本 Blender 没有 temp_override
        return call(**kwargs)


def _vec(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return (float(v[0]), float(v[1]), float(v[2]))
    return default


def _apply_transform(obj):
    """把变换烘进网格数据。"""
    _activate(obj)
    _ops(bpy.ops.object.transform_apply, location=False, rotation=True, scale=True)


# ══════════════════════════════════════════════════════ 场景查询 ══

def cmd_get_scene_info(params):
    sc = bpy.context.scene
    detail = params.get('detail', 'normal')
    include_hidden = params.get('include_hidden', False)
    limit = int(params.get('max_objects') or 50)

    objs = [o for o in sc.objects if include_hidden or not o.hide_viewport]
    out_objs = []
    for o in objs[:limit]:
        item = {'name': o.name, 'type': o.type}
        if detail != 'brief':
            item['location'] = [round(v, 4) for v in o.location]
            item['dimensions'] = [round(v, 4) for v in o.dimensions]
            item['visible'] = not o.hide_viewport
        if detail == 'full' and o.type == 'MESH':
            item['vertices'] = len(o.data.vertices)
            item['faces'] = len(o.data.polygons)
            item['materials'] = [s.material.name for s in o.material_slots if s.material]
            item['modifiers'] = [m.type for m in o.modifiers]
        out_objs.append(item)

    active = bpy.context.view_layer.objects.active
    return {
        'scene': sc.name,
        'object_count': len(objs),
        'returned': len(out_objs),
        'truncated': len(objs) > limit,
        'objects': out_objs,
        'active_object': active.name if active else None,
        'selected': [o.name for o in sc.objects if o.select_get()],
        'collections': [c.name for c in bpy.data.collections],
        'materials_count': len(bpy.data.materials),
        'render_engine': sc.render.engine,
        'frame': sc.frame_current,
    }


def cmd_get_object_info(params):
    o = _obj(params['name'])
    info = {
        'name': o.name,
        'type': o.type,
        'location': [round(v, 6) for v in o.location],
        'rotation': [round(v, 6) for v in o.rotation_euler],
        'scale': [round(v, 6) for v in o.scale],
        'dimensions': [round(v, 6) for v in o.dimensions],
        'parent': o.parent.name if o.parent else None,
        'collections': [c.name for c in o.users_collection],
        'hide_viewport': o.hide_viewport,
        'hide_render': o.hide_render,
        'custom_props': {k: str(v)[:200] for k, v in o.items() if not k.startswith('_')},
    }
    if o.type == 'MESH':
        info.update({
            'vertices': len(o.data.vertices),
            'edges': len(o.data.edges),
            'faces': len(o.data.polygons),
            'materials': [s.material.name for s in o.material_slots if s.material],
            'modifiers': [{'name': m.name, 'type': m.type} for m in o.modifiers],
            'bounding_box': [[round(v, 6) for v in c] for c in o.bound_box],
        })
    return info


def cmd_get_blender_version(params):
    return {
        'blender': bpy.app.version_string,
        'blender_int': list(bpy.app.version),
        'python': '%d.%d.%d' % tuple(bpy.app.version[:0] or (0, 0, 0)) or None,
        'addon_version': '%d.%d.%d' % bl_info['version'],
        'engines': [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items],
        'filepath': bpy.data.filepath or None,
    }


# ══════════════════════════════════════════════════════ 创建几何 ══

PRIMITIVE_OPS = {
    'cube': bpy.ops.mesh.primitive_cube_add,
    'sphere': bpy.ops.mesh.primitive_uv_sphere_add,
    'ico_sphere': bpy.ops.mesh.primitive_ico_sphere_add,
    'cylinder': bpy.ops.mesh.primitive_cylinder_add,
    'cone': bpy.ops.mesh.primitive_cone_add,
    'torus': bpy.ops.mesh.primitive_torus_add,
    'plane': bpy.ops.mesh.primitive_plane_add,
    'circle': bpy.ops.mesh.primitive_circle_add,
    'grid': bpy.ops.mesh.primitive_grid_add,
    'monkey': bpy.ops.mesh.primitive_monkey_add,
}


def cmd_add_primitive(params):
    ptype = params.get('type', 'cube')
    op = PRIMITIVE_OPS.get(ptype)
    if op is None:
        raise ValueError('不支持的基本体「%s」。可用：%s' % (ptype, ', '.join(PRIMITIVE_OPS)))

    loc = _vec(params.get('location'), (0, 0, 0))
    rot = _vec(params.get('rotation'), (0, 0, 0))
    kwargs = {'location': loc, 'rotation': rot}

    size = params.get('size')
    if size is not None:
        if ptype in ('cube', 'plane', 'grid'):
            kwargs['size'] = float(size)
        else:
            kwargs['radius'] = float(size) / 2.0

    if params.get('radius') is not None:
        kwargs['radius'] = float(params['radius'])
    if params.get('depth') is not None and ptype in ('cylinder', 'cone'):
        kwargs['depth'] = float(params['depth'])
    if params.get('segments') is not None and ptype in ('sphere', 'ico_sphere', 'torus'):
        if ptype == 'torus':
            kwargs['major_segments'] = int(params['segments'])
        else:
            kwargs['segments'] = int(params['segments'])
    if params.get('vertices') is not None and ptype in ('cylinder', 'cone', 'circle'):
        kwargs['vertices'] = int(params['vertices'])

    try:
        _ops(op, **kwargs)
    except TypeError:
        # 某些 Blender 版本的参数名不同，退回最小参数集
        _ops(op, location=loc, rotation=rot)

    o = _active()
    if o is None:
        # 后台模式或上下文异常时，从场景里找刚加进来的对象
        candidates = [x for x in bpy.context.scene.objects if x.type == 'MESH']
        o = candidates[-1] if candidates else None
    if o is None:
        raise RuntimeError('基本体创建后找不到对象（上下文异常）')
    if params.get('name'):
        o.name = params['name']
    if params.get('scale'):
        s = _vec(params['scale'])
        if s:
            o.scale = s

    return {
        'name': o.name,
        'type': o.type,
        'location': [round(v, 4) for v in o.location],
        'dimensions': [round(v, 4) for v in o.dimensions],
    }


def cmd_add_text(params):
    _ops(bpy.ops.object.text_add, location=_vec(params.get('location'), (0, 0, 0)))
    o = _active()
    if params.get('name'):
        o.name = params['name']
    o.data.body = params.get('text', 'Text')
    if params.get('size') is not None:
        o.data.size = float(params['size'])
    if params.get('extrude') is not None:
        o.data.extrude = float(params['extrude'])
    if params.get('rotation'):
        o.rotation_euler = _vec(params['rotation'], (0, 0, 0))
    if params.get('align_x'):
        o.data.align_x = params['align_x']
    if params.get('align_y'):
        o.data.align_y = params['align_y']
    return {'name': o.name, 'text': o.data.body}


def cmd_add_empty(params):
    _ops(bpy.ops.object.empty_add, location=_vec(params.get('location'), (0, 0, 0)))
    o = _active()
    if params.get('name'):
        o.name = params['name']
    if params.get('display_type'):
        o.empty_display_type = params['display_type']
    if params.get('size') is not None:
        o.empty_display_size = float(params['size'])
    return {'name': o.name}


# ══════════════════════════════════════════════════════ 编辑几何 ══

def cmd_extrude(params):
    o = _obj(params['name'])
    distance = float(params.get('distance', 0.1))
    axis = params.get('axis', 'NORMAL')
    face_idx = params.get('faces')

    from mathutils import Vector
    bm = bmesh.new()
    try:
        bm.from_mesh(o.data)
        bm.faces.ensure_lookup_table()
        if face_idx:
            sel = [bm.faces[i] for i in face_idx if 0 <= i < len(bm.faces)]
        else:
            sel = list(bm.faces)
        if not sel:
            raise ValueError('对象「%s」没有可挤出的面' % o.name)

        # 先算出每个面要移动的方向
        if axis == 'NORMAL':
            dirs = {f.index: f.normal.copy() for f in sel}
        else:
            v = Vector({'X': (1, 0, 0), 'Y': (0, 1, 0), 'Z': (0, 0, 1)}[axis])
            dirs = {f.index: v for f in sel}

        # 记下每个选中面原有顶点位置，挤出后按方向平移
        ret = bmesh.ops.extrude_face_region(bm, geom=sel)
        new_verts = [e for e in ret['geom'] if isinstance(e, bmesh.types.BMVert)]
        # 用原始面的中心方向决定平移量（挤出后新面法线仍是原朝向）
        for v in new_verts:
            v.co += v.normal * distance if axis == 'NORMAL' else \
                Vector({'X': (distance, 0, 0), 'Y': (0, distance, 0),
                        'Z': (0, 0, distance)}[axis])

        bm.normal_update()
        bm.to_mesh(o.data)
        o.data.update()
    finally:
        bm.free()

    return {'name': o.name, 'distance': distance, 'axis': axis,
            'faces_after': len(o.data.polygons)}


def cmd_inset(params):
    o = _obj(params['name'])
    thickness = float(params.get('thickness', 0.02))
    depth = float(params.get('depth', 0.0))

    bm = bmesh.new()
    try:
        bm.from_mesh(o.data)
        faces = list(bm.faces)
        if not faces:
            raise ValueError('对象「%s」没有面可以内插' % o.name)
        ret = bmesh.ops.inset_region(bm, faces=faces, thickness=thickness, depth=depth,
                                     use_even_offset=True)
        bm.to_mesh(o.data)
        o.data.update()
    finally:
        bm.free()

    return {'name': o.name, 'thickness': thickness, 'depth': depth,
            'faces_after': len(o.data.polygons)}


def cmd_bevel(params):
    o = _obj(params['name'])
    width = float(params.get('width', 0.01))
    segments = int(params.get('segments', 2))
    affect = params.get('affect', 'EDGES')

    bm = bmesh.new()
    try:
        bm.from_mesh(o.data)
        if affect == 'VERTICES':
            geom = list(bm.verts)
        else:
            geom = list(bm.edges)
        if not geom:
            raise ValueError('对象「%s」没有可倒角的几何' % o.name)
        bmesh.ops.bevel(bm, geom=geom, offset=width, segments=segments,
                        affect=affect, profile=0.5, clamp_overlap=True)
        bm.to_mesh(o.data)
        o.data.update()
    finally:
        bm.free()

    return {'name': o.name, 'width': width, 'segments': segments,
            'faces_after': len(o.data.polygons)}


def cmd_subdivide(params):
    o = _obj(params['name'])
    cuts = int(params.get('cuts', 1))

    bm = bmesh.new()
    try:
        bm.from_mesh(o.data)
        edges = list(bm.edges)
        if not edges:
            raise ValueError('对象「%s」没有边可以细分' % o.name)
        bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts, use_grid_fill=True)
        bm.to_mesh(o.data)
        o.data.update()
    finally:
        bm.free()

    return {'name': o.name, 'cuts': cuts, 'faces_after': len(o.data.polygons)}


def cmd_loop_cut(params):
    o = _obj(params['name'])
    axis = params.get('axis', 'Z')
    count = int(params.get('count', 1))
    offset = float(params.get('offset', 0.5))

    from mathutils import Vector
    bb = [o.matrix_world @ Vector(c) for c in o.bound_box]
    ai = {'X': 0, 'Y': 1, 'Z': 2}[axis]
    lo = min(v[ai] for v in bb)
    hi = max(v[ai] for v in bb)

    # 把切面从世界坐标转回局部坐标
    inv = o.matrix_world.inverted()
    bm = bmesh.new()
    try:
        bm.from_mesh(o.data)
        for i in range(count):
            t = offset if count == 1 else (i + 1) / (count + 1.0)
            coord = lo + (hi - lo) * t
            world_co = [0.0, 0.0, 0.0]
            world_co[ai] = coord
            local_co = inv @ Vector(world_co)
            normal = Vector((0.0, 0.0, 0.0))
            normal[ai] = 1.0
            local_no = (inv.to_3x3() @ normal).normalized()
            geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
            bmesh.ops.bisect_plane(bm, geom=geom, plane_co=local_co, plane_no=local_no,
                                   clear_inner=False, clear_outer=False)
        bm.to_mesh(o.data)
        o.data.update()
    finally:
        bm.free()

    return {'name': o.name, 'axis': axis, 'count': count, 'faces_after': len(o.data.polygons)}


def cmd_smooth_shade(params):
    o = _obj(params['name'])
    smooth = params.get('smooth', True)
    _activate(o)
    _mode('OBJECT')
    if smooth:
        _ops(bpy.ops.object.shade_smooth)
    else:
        _ops(bpy.ops.object.shade_flat)
    angle = params.get('auto_smooth_angle')
    if angle is not None:
        try:
            o.data.use_auto_smooth = True
            o.data.auto_smooth_angle = math.radians(float(angle))
        except AttributeError:
            # Blender 4.1+ 移除了 use_auto_smooth，改用修改器
            pass
    return {'name': o.name, 'smooth': smooth}


# ══════════════════════════════════════════════════════ 变换对齐 ══

def cmd_transform_object(params):
    o = _obj(params['name'])
    mode = params.get('mode', 'SET')

    for key, attr in (('location', 'location'), ('rotation', 'rotation_euler'), ('scale', 'scale')):
        v = _vec(params.get(key))
        if v is None:
            continue
        if mode == 'ADD':
            cur = list(getattr(o, attr))
            setattr(o, attr, [cur[i] + v[i] for i in range(3)])
        elif mode == 'MULTIPLY':
            cur = list(getattr(o, attr))
            setattr(o, attr, [cur[i] * v[i] for i in range(3)])
        else:
            setattr(o, attr, v)

    return {
        'name': o.name,
        'location': [round(v, 6) for v in o.location],
        'rotation': [round(v, 6) for v in o.rotation_euler],
        'scale': [round(v, 6) for v in o.scale],
    }


def cmd_align_objects(params):
    names = params.get('names') or []
    if not names:
        raise ValueError('names 不能为空')
    axis = params.get('axis', 'X')
    align = params.get('align', 'CENTER')
    ai = {'X': 0, 'Y': 1, 'Z': 2}[axis]

    objs = [_obj(n) for n in names]
    vals = []
    for o in objs:
        c = o.matrix_world.translation[ai]
        half = o.dimensions[ai] / 2.0
        if align == 'MIN':
            vals.append(c - half)
        elif align == 'MAX':
            vals.append(c + half)
        else:
            vals.append(c)

    target = sum(vals) / len(vals) if align == 'CENTER' else (
        min(vals) if align == 'MIN' else max(vals))

    for o, cur in zip(objs, vals):
        delta = target - cur
        loc = list(o.location)
        loc[ai] += delta
        o.location = loc

    return {'aligned': [o.name for o in objs], 'axis': axis, 'align': align,
            'target': round(target, 6)}


def cmd_mirror_object(params):
    o = _obj(params['name'])
    axis = params.get('axis', 'X')
    duplicate = params.get('duplicate', True)
    point = _vec(params.get('mirror_point'), (0, 0, 0))

    _activate(o)
    if duplicate:
        _ops(bpy.ops.object.duplicate)
        o = _active()
        o.name = params['name'] + '_mirrored'

    ai = {'X': 0, 'Y': 1, 'Z': 2}[axis]
    scale = list(o.scale)
    scale[ai] *= -1
    o.scale = scale
    # 把镜像中心也考虑进去
    loc = list(o.location)
    loc[ai] = 2 * point[ai] - loc[ai]
    o.location = loc

    return {'name': o.name, 'axis': axis, 'duplicated': duplicate}


def cmd_duplicate_object(params):
    o = _obj(params['name'])
    count = int(params.get('count', 1))
    offset = _vec(params.get('offset'), (0, 0, 0))
    linked = params.get('linked', False)

    made = []
    for i in range(count):
        _activate(o)
        if linked:
            _ops(bpy.ops.object.duplicate, linked=True)
        else:
            _ops(bpy.ops.object.duplicate, linked=False)
        dup = _active()
        dup.location = [o.location[j] + offset[j] * (i + 1) for j in range(3)]
        made.append(dup.name)
    return {'source': o.name, 'created': made}


# ══════════════════════════════════════════════════════ 修改器 ══

def cmd_add_modifier(params):
    o = _obj(params['name'])
    mtype = params.get('type')
    props = params.get('props') or {}

    _activate(o)
    m = o.modifiers.new(name=mtype.title(), type=mtype)

    for k, v in props.items():
        try:
            if k == 'object' and isinstance(v, str):
                setattr(m, k, _obj(v))
            elif hasattr(m, k):
                setattr(m, k, v)
        except Exception:
            pass

    if params.get('apply'):
        _activate(o)
        try:
            _ops(bpy.ops.object.modifier_apply, modifier=m.name)
            return {'name': o.name, 'modifier': m.name, 'applied': True}
        except Exception as e:
            return {'name': o.name, 'modifier': m.name, 'applied': False, 'apply_error': str(e)}

    return {'name': o.name, 'modifier': m.name, 'type': mtype,
            'props': {k: str(getattr(m, k))[:80] for k in props if hasattr(m, k)}}


def cmd_list_modifiers(params):
    o = _obj(params['name'])
    out = []
    for m in o.modifiers:
        item = {'name': m.name, 'type': m.type, 'show_viewport': m.show_viewport}
        for attr in ('levels', 'width', 'count', 'thickness', 'operation', 'segments'):
            if hasattr(m, attr):
                try:
                    item[attr] = getattr(m, attr)
                except Exception:
                    pass
        out.append(item)
    return {'name': o.name, 'modifiers': out}


def cmd_remove_modifier(params):
    o = _obj(params['name'])
    mname = params.get('modifier')
    action = params.get('action', 'REMOVE')
    _activate(o)
    if action == 'APPLY':
        _ops(bpy.ops.object.modifier_apply, modifier=mname)
        return {'name': o.name, 'modifier': mname, 'applied': True}
    _ops(bpy.ops.object.modifier_remove, modifier=mname)
    return {'name': o.name, 'modifier': mname, 'removed': True}


# ══════════════════════════════════════════════════════ 材质 ══

def _principled(mat):
    """取材质的 Principled BSDF 节点。

    **必须按 type 找，不能按 name 找**：Blender 的节点名会跟随界面语言本地化。
    中文界面下节点叫「原理化 BSDF」，英文界面下叫 "Principled BSDF"。
    按 name 取会在非英文环境返回 None，然后 a.inputs 就报
    'NoneType' object has no attribute 'inputs' —— 这是真实踩过的坑。

    node.type 是稳定的标识符（'BSDF_PRINCIPLED'），不受语言影响。
    """
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for n in mat.node_tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            return n
    # 没有就建一个并接到输出上（有些材质是空的）
    try:
        node = mat.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
        out = next((n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if out is None:
            out = mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
        mat.node_tree.links.new(node.outputs['BSDF'], out.inputs['Surface'])
        return node
    except Exception:
        return None


def _socket(node, *names):
    """按名字取 socket，取不到返回 None。

    socket 的 name 也可能本地化，但 identifier 不会。所以先按 name 试，
    再用 identifier 兜底。输入的 identifier 和英文 name 目前是一致的。
    """
    if node is None:
        return None
    for wanted in names:
        for sock in node.inputs:
            if sock.name == wanted or sock.identifier == wanted:
                return sock
    return None


def _color4(v):
    """把 [r,g,b] 或 [r,g,b,a] 或单个数值转成 4 元组。返回 None 表示未提供。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return (float(v),) * 3 + (1.0,)
    try:
        vals = list(v)
    except Exception:
        return None
    if not vals:
        return None
    if len(vals) == 3:
        vals.append(1.0)
    return tuple(float(x) for x in vals[:4])


def cmd_create_material(params):
    name = params.get('name', 'Material')
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = _principled(mat)

    def _color(v):
        if not v:
            return None
        v = list(v)
        if len(v) == 3:
            v.append(1.0)
        return tuple(float(x) for x in v[:4])

    if bsdf:
        c = _color(params.get('base_color'))
        if c:
            sock = _socket(bsdf, 'Base Color')
            if sock:
                sock.default_value = c
        if params.get('metallic') is not None:
            sock = _socket(bsdf, 'Metallic')
            if sock:
                sock.default_value = float(params['metallic'])
        if params.get('roughness') is not None:
            sock = _socket(bsdf, 'Roughness')
            if sock:
                sock.default_value = float(params['roughness'])
        if params.get('alpha') is not None:
            sock = _socket(bsdf, 'Alpha')
            if sock:
                sock.default_value = float(params['alpha'])
            if hasattr(mat, 'blend_method'):
                mat.blend_method = 'BLEND' if float(params['alpha']) < 1.0 else 'OPAQUE'
        ec = _color(params.get('emission_color'))
        if ec:
            sock = _socket(bsdf, 'Emission Color', 'Emission')
            if sock:
                sock.default_value = ec
        if params.get('emission_strength') is not None:
            sock = _socket(bsdf, 'Emission Strength')
            if sock:
                sock.default_value = float(params['emission_strength'])

    return {'material': mat.name, 'users': mat.users, 'has_bsdf': bsdf is not None}


def cmd_assign_material(params):
    o = _obj(params['object'])
    mat = bpy.data.materials.get(params['material'])
    if mat is None:
        raise ValueError('找不到材质「%s」' % params['material'])
    slot = int(params.get('slot', 0))

    if len(o.material_slots) <= slot:
        o.data.materials.append(mat)
    else:
        o.material_slots[slot].material = mat
    return {'object': o.name, 'material': mat.name, 'slot': slot,
            'slots_total': len(o.material_slots)}


def cmd_set_material_property(params):
    mat = bpy.data.materials.get(params['material'])
    if mat is None:
        raise ValueError('找不到材质「%s」' % params['material'])
    bsdf = _principled(mat)
    if bsdf is None:
        raise ValueError('材质「%s」里没有 Principled BSDF 节点' % mat.name)

    if params.get('base_color'):
        c = list(params['base_color'])
        if len(c) == 3:
            c.append(1.0)
        sock = _socket(bsdf, 'Base Color')
        if sock:
            sock.default_value = tuple(c[:4])
    for key, inp in (('metallic', 'Metallic'), ('roughness', 'Roughness'), ('alpha', 'Alpha')):
        if params.get(key) is not None:
            sock = _socket(bsdf, inp)
            if sock:
                sock.default_value = float(params[key])
    if params.get('emission_strength') is not None:
        sock = _socket(bsdf, 'Emission Strength')
        if sock:
            sock.default_value = float(params['emission_strength'])
    return {'material': mat.name, 'updated': True}


def cmd_list_materials(params):
    out = []
    for m in bpy.data.materials:
        users = [o.name for o in bpy.data.objects if o.type == 'MESH'
                 and any(s.material == m for s in o.material_slots)]
        item = {'name': m.name, 'users': users[:10], 'user_count': len(users)}
        bsdf = _principled(m) if m.use_nodes else None
        if bsdf:
            bc = _socket(bsdf, 'Base Color')
            mt = _socket(bsdf, 'Metallic')
            rg = _socket(bsdf, 'Roughness')
            if bc:
                item['base_color'] = [round(v, 3) for v in bc.default_value]
            if mt:
                item['metallic'] = round(mt.default_value, 3)
            if rg:
                item['roughness'] = round(rg.default_value, 3)
        out.append(item)
    return {'materials': out, 'count': len(out)}


# ══════════════════════════════════════════════════════ 物品分组 ══

def _find_collection(name):
    """按名字找 collection。"""
    target = str(name)
    coll = bpy.data.collections.get(target)
    if coll is not None:
        return coll
    for c in bpy.data.collections:
        if c.name == target:
            return c
    return None


def _collection_objects(coll):
    """一个 collection 直接包含的对象（不含子 collection 的）。"""
    return [o for o in coll.objects]


def cmd_group_create(params):
    """把一个**完整物品**的所有零件归入一个 Collection。

    为什么用 Collection 而不是命名约定：
      名字前缀（`桌_桌面`）只是**推断**，容易误判——
      改成 `桌面`、`桌腿` 这种不带分隔符的命名，推断就失效了。
      Collection 是 Blender 的**原生分组机制**：
        - 真实的层级结构，Outliner 里看得见
        - 每个物品独立一个组，组内零件共享一个父节点
        - 渲染、隐藏、导出都能按组操作
      所以现在的规则是：**一个完整物品 = 一个 Collection，零件只需命名。**

    分组后能做的事：
      - 检查按组跑（组内穿模 / 组间穿模分开算）
      - 五视图按组隔离渲染
      - 精度一致性按组比较
    """
    group = params.get('group') or params.get('name')
    if not group:
        raise ValueError('必须给 group（物品组名）')

    objects = params.get('objects') or []
    if not objects:
        raise ValueError('必须给 objects（这个物品的零件对象名列表）')

    coll = _find_collection(group)
    created = False
    if coll is None:
        coll = bpy.data.collections.new(str(group))
        bpy.context.scene.collection.children.link(coll)
        created = True

    moved = []
    missing = []
    for name in objects:
        o = bpy.data.objects.get(str(name))
        if o is None:
            missing.append(str(name))
            continue
        # 从「场景直属」摘掉，避免同时挂在两处
        try:
            if o.name in bpy.context.scene.collection.objects:
                bpy.context.scene.collection.objects.unlink(o)
        except Exception:
            pass
        # 从别的 collection 摘掉（一个物品只属于一个组）
        for c in list(bpy.data.collections):
            if c.name == coll.name:
                continue
            try:
                if o.name in c.objects:
                    c.objects.unlink(o)
            except Exception:
                pass
        if o.name not in coll.objects:
            coll.objects.link(o)
        moved.append(o.name)

    return {
        'group': coll.name,
        'created': created,
        'objects': moved,
        'missing': missing,
        'total_in_group': len(_collection_objects(coll)),
    }


def cmd_group_list(params):
    """列出所有物品组及组内零件，以及**没有分组的 mesh**。"""
    out = []
    scene_root = bpy.context.scene.collection
    import mathutils

    for coll in bpy.data.collections:
        meshes = [o for o in _collection_objects(coll) if o.type == 'MESH']
        if not meshes and not params.get('include_empty'):
            continue
        mn = [1e18, 1e18, 1e18]
        mx = [-1e18, -1e18, -1e18]
        for o in meshes:
            for c in o.bound_box:
                w = o.matrix_world @ mathutils.Vector(c)
                for i in range(3):
                    mn[i] = min(mn[i], w[i])
                    mx[i] = max(mx[i], w[i])
        dims = [mx[i] - mn[i] for i in range(3)] if meshes else [0, 0, 0]
        out.append({
            'name': coll.name,
            'parts': [o.name for o in meshes],
            'part_count': len(meshes),
            'dims': [round(d, 4) for d in dims],
            'hidden': bool(coll.hide_viewport),
            'in_scene': coll.name in [c.name for c in scene_root.children],
        })

    grouped = set()
    for coll in bpy.data.collections:
        for o in _collection_objects(coll):
            grouped.add(o.name)
    ungrouped = [o.name for o in bpy.context.scene.objects
                 if o.type == 'MESH' and o.name not in grouped]

    return {
        'groups': out,
        'group_count': len(out),
        'ungrouped': ungrouped,
        'ungrouped_count': len(ungrouped),
    }


def cmd_group_split(params):
    """按命名前缀自动推断分组，批量建 Collection。

    用途：已经建好一堆零件、且命名规范（`桌_桌面`）时，
    一次把所有物品的组都建出来，不用逐个调 group_create。
    """
    mode = str(params.get('mode', 'prefix')).lower()
    dry_run = bool(params.get('dry_run', False))
    import re

    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']

    def prefix_of(name):
        m = re.match(r'^(.+?)[_\-.|:/\\ ]', str(name))
        if m and m.group(1):
            return m.group(1)
        m2 = re.match(r'^([A-Z][a-z]+)', str(name))
        if m2:
            return m2.group(1)
        return None

    buckets = {}
    for o in meshes:
        if mode == 'parent':
            p = o.parent
            key = p.name if p else None
        else:
            key = prefix_of(o.name)
        if not key:
            key = '__ungrouped__'
        buckets.setdefault(key, []).append(o.name)

    groups = {k: v for k, v in buckets.items() if k != '__ungrouped__'}

    if dry_run:
        return {
            'dry_run': True,
            'groups': [{'name': k, 'parts': v, 'count': len(v)} for k, v in groups.items()],
            'ungrouped': buckets.get('__ungrouped__', []),
        }

    results = []
    for name, parts in groups.items():
        try:
            results.append(cmd_group_create({'group': name, 'objects': parts}))
        except Exception as e:
            results.append({'group': name, 'error': str(e)[:150]})

    return {
        'mode': mode,
        'created_groups': len(results),
        'results': results,
        'ungrouped': buckets.get('__ungrouped__', []),
    }


def cmd_group_select(params):
    """选中一个物品组的全部零件（便于按组批量操作）。"""
    group = params.get('group')
    if not group:
        raise ValueError('必须给 group')
    coll = _find_collection(group)
    if coll is None:
        raise ValueError('找不到物品组：%s' % group)

    _deselect_all()
    sel = []
    for o in _collection_objects(coll):
        if o.type == 'MESH':
            try:
                o.select_set(True)
                sel.append(o.name)
            except Exception:
                pass
    return {'group': coll.name, 'selected': sel, 'count': len(sel)}


def cmd_group_delete(params):
    """删除物品组。默认只解除分组（保留对象）。"""
    group = params.get('group')
    if not group:
        raise ValueError('必须给 group')
    coll = _find_collection(group)
    if coll is None:
        raise ValueError('找不到物品组：%s' % group)

    keep_objects = params.get('keep_objects', True)
    names = [o.name for o in _collection_objects(coll)]

    if keep_objects:
        for name in names:
            o = bpy.data.objects.get(name)
            if o is None:
                continue
            try:
                if o.name not in bpy.context.scene.collection.objects:
                    bpy.context.scene.collection.objects.link(o)
            except Exception:
                pass
        try:
            bpy.data.collections.remove(coll)
        except Exception as e:
            raise ValueError('删除组失败：%s' % e)
        return {'group': str(group), 'deleted': True, 'objects_kept': names}

    for name in names:
        o = bpy.data.objects.get(name)
        if o is not None:
            bpy.data.objects.remove(o, do_unlink=True)
    try:
        bpy.data.collections.remove(coll)
    except Exception:
        pass
    return {'group': str(group), 'deleted': True, 'objects_removed': names}


# ══════════════════════════════════════════════════════ UV 映射 ══

def _uv_stats(obj):
    """统计一个对象的 UV 质量：有多少 UV 层、UV 面积利用率、有多少重叠。"""
    me = obj.data
    if not me.uv_layers:
        return {'has_uv': False, 'layers': 0}

    layer = me.uv_layers.active or me.uv_layers[0]
    uvs = layer.data
    if len(uvs) == 0:
        return {'has_uv': True, 'layers': len(me.uv_layers), 'empty': True}

    # UV 包围盒与总面积
    us = [uvs[i].uv[0] for i in range(len(uvs))]
    vs = [uvs[i].uv[1] for i in range(len(uvs))]
    u0, u1 = min(us), max(us)
    v0, v1 = min(vs), max(vs)

    # 三角面在 UV 空间的总面积
    total_uv_area = 0.0
    for poly in me.polygons:
        if len(poly.loop_indices) < 3:
            continue
        pts = [(uvs[li].uv[0], uvs[li].uv[1]) for li in poly.loop_indices]
        for k in range(1, len(pts) - 1):
            ax, ay = pts[0]
            bx, by = pts[k]
            cx, cy = pts[k + 1]
            total_uv_area += abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) * 0.5

    bbox_w = max(1e-9, u1 - u0)
    bbox_h = max(1e-9, v1 - v0)
    coverage = total_uv_area / (bbox_w * bbox_h) if bbox_w * bbox_h > 0 else 0

    # 是否超出 0~1（超出会导致纹理平铺或裁切，这是常见错误）
    outside = u0 < -0.001 or v0 < -0.001 or u1 > 1.001 or v1 > 1.001

    return {
        'has_uv': True,
        'layers': len(me.uv_layers),
        'layer_name': layer.name,
        'uv_bbox': [round(u0, 4), round(v0, 4), round(u1, 4), round(v1, 4)],
        'uv_area': round(total_uv_area, 6),
        # 利用率：接近 1 = 铺满，很低 = 浪费贴图分辨率
        'utilization': round(coverage, 4),
        'outside_unit': outside,
        'islands': None,
    }


def cmd_uv_unwrap(params):
    """给对象展 UV。**这是 PBR 贴图精度的前提。**

    不做 UV 直接用程序化纹理，只能靠 Object/Generated 坐标——
    那会导致所有材质共用同一套坐标映射，纹理在不同物体上密度不一致。
    做了 UV 才能精确控制每个面的贴图密度。

    支持多种展开方式，各有适用场景：
      smart_project — 自动分块，万金油（默认）
      angle_based   — 按角度分缝，适合硬表面机械件
      conformal     — 保角展开，适合有机形状
      sphere        — 球面投影，适合球形
      cylinder      — 柱面投影，适合管状
      cube          — 立方投影，适合方块
    """
    name = params.get('object')
    obj = _obj(name) if name else _active()
    if obj is None or obj.type != 'MESH':
        raise ValueError('需要一个 mesh 对象')

    method = str(params.get('method', 'smart_project')).lower()
    angle = float(params.get('angle_limit', 66.0))
    island_margin = float(params.get('island_margin', 0.02))
    area_weight = float(params.get('area_weight', 0.0))
    correct_aspect = bool(params.get('correct_aspect', True))
    scale_to_bounds = bool(params.get('scale_to_bounds', True))

    # 必须先选中且进入编辑模式
    _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        _ops(bpy.ops.mesh.select_all, action='SELECT')

        if method == 'smart_project':
            _ops(bpy.ops.uv.smart_project,
                 angle_limit=angle * 3.14159265 / 180.0,
                 island_margin=island_margin,
                 area_weight=area_weight,
                 correct_aspect=correct_aspect,
                 scale_to_bounds=scale_to_bounds)
        elif method == 'angle_based':
            _ops(bpy.ops.uv.unwrap, method='ANGLE_BASED',
                 margin=island_margin)
        elif method == 'conformal':
            _ops(bpy.ops.uv.unwrap, method='CONFORMAL',
                 margin=island_margin, fill_holes=True)
        elif method == 'sphere':
            _ops(bpy.ops.uv.sphere_project, correct_aspect=correct_aspect)
        elif method == 'cylinder':
            _ops(bpy.ops.uv.cylinder_project, correct_aspect=correct_aspect)
        elif method == 'cube':
            _ops(bpy.ops.uv.cube_project, correct_aspect=correct_aspect)
        else:
            raise ValueError('不支持的 method：%s' % method)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    st = _uv_stats(obj)
    return {
        'object': obj.name,
        'method': method,
        'uv': st,
        'tip': ('UV 利用率 ' + str(st.get('utilization')) +
                '。低于 0.5 说明贴图分辨率有很大浪费，'
                '可以考虑 pack_islands 重新排布。')
        if st.get('has_uv') else 'UV 没建出来',
    }


def cmd_uv_pack(params):
    """重新排列 UV 岛，提高贴图利用率。

    用途：unwrap 之后 UV 岛是散落的，有很多空白。
    pack 会把它们紧凑排布，同一张贴图能覆盖更多细节——
    相当于**免费提高贴图精度**。
    """
    name = params.get('object')
    obj = _obj(name) if name else _active()
    if obj is None or obj.type != 'MESH':
        raise ValueError('需要一个 mesh 对象')

    margin = float(params.get('margin', 0.003))
    rotate = bool(params.get('rotate', True))
    merge_overlap = bool(params.get('merge_overlap', False))

    _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        _ops(bpy.ops.mesh.select_all, action='SELECT')
        # Blender 3.6+ 的新 pack_islands，参数更丰富
        try:
            _ops(bpy.ops.uv.pack_islands,
                 rotate=rotate, margin=margin, merge_overlap=merge_overlap)
        except TypeError:
            # 老版本没有对这些参数的支持
            _ops(bpy.ops.uv.pack_islands, margin=margin)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {'object': obj.name, 'uv': _uv_stats(obj)}


def cmd_uv_scale(params):
    """按物理尺寸统一 UV 密度（texel density）。

    **这是「不同物体贴图密度一致」的关键。**
    不做这一步，同样一张 1024 贴图铺在 10cm 的螺丝和 2m 的墙上，
    密度差 20 倍——看起来就是一个精细一个糊。

    texel_density 表示「多少个像素对应一个世界单位」。
    比如 512 表示 1 单位长度用 512 像素，那 2 单位的物体就需要 1024 贴图。
    """
    name = params.get('object')
    obj = _obj(name) if name else _active()
    if obj is None or obj.type != 'MESH':
        raise ValueError('需要一个 mesh 对象')

    density = float(params.get('texel_density', 512.0))
    texture_size = int(params.get('texture_size', 1024))

    me = obj.data
    if not me.uv_layers:
        raise ValueError('对象没有 UV，先调 uv_unwrap')

    # 世界尺度下这个物体的表面积
    area = 0.0
    import mathutils
    mw = obj.matrix_world
    for poly in me.polygons:
        pts = [mw @ me.vertices[vi].co for vi in poly.vertices]
        for k in range(1, len(pts) - 1):
            area += (pts[k] - pts[0]).cross(pts[k + 1] - pts[0]).length * 0.5

    if area <= 0:
        raise ValueError('对象表面积为 0，无法计算 UV 密度')

    uv_layer = me.uv_layers.active or me.uv_layers[0]
    uvs = uv_layer.data

    # 当前 UV 面积
    uv_area = 0.0
    for poly in me.polygons:
        if len(poly.loop_indices) < 3:
            continue
        pts = [(uvs[li].uv[0], uvs[li].uv[1]) for li in poly.loop_indices]
        for k in range(1, len(pts) - 1):
            ax, ay = pts[0]
            bx, by = pts[k]
            cx, cy = pts[k + 1]
            uv_area += abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) * 0.5

    if uv_area <= 0:
        raise ValueError('UV 面积为 0，UV 可能完全退化')

    # ── 正确的 texel density 计算 ──
    #
    # 定义：texel_density = 每「世界单位长度」用多少像素。
    #
    # 推导：UV 空间是 0~1，贴图是 texture_size 像素。
    # 所以 UV 空间 1.0 长度 = texture_size 像素。
    # 要让「世界 1 单位」对应 density 像素：
    #   世界 1 单位 → UV 空间 (density / texture_size)
    #   面积同理：世界面积 A → UV 面积 A × (density/texture_size)²
    #
    # 所以目标 UV 面积 = 世界面积 × (density / texture_size)²
    #
    # 但 UV 空间上限是 1.0（不能超出 0~1，否则平铺）。
    # 如果算出的目标面积 > 1，说明这个物体太大、给的 density 太高，
    # 一张贴图装不下——这时必须**降低 density**，而不是让 UV 溢出。
    scale_uv = (density / texture_size) ** 2
    ideal_uv_area = area * scale_uv
    capped = False
    target_uv_area = ideal_uv_area
    if target_uv_area > 0.98:
        # 装不下：把 UV 撑满 0~1，并如实报告实际达到的密度
        target_uv_area = 0.98
        capped = True

    factor = (target_uv_area / uv_area) ** 0.5
    # 实际能达到的密度（受 UV 空间上限约束）
    achieved = (target_uv_area / max(1e-12, area)) ** 0.5 * texture_size

    _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        _ops(bpy.ops.mesh.select_all, action='SELECT')
        # 以 UV 中心缩放
        _ops(bpy.ops.uv.select_all, action='SELECT')
        bpy.ops.transform.resize(value=(factor, factor, 1.0))
        # 注意：**不在这里 pack**——pack 会把刚设好的密度重置掉。
        # 密度和排布是两件事，pack 由调用方决定是否单独做。
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    st = _uv_stats(obj)
    return {
        'object': obj.name,
        'requested_density': density,
        'achieved_density': round(achieved, 1),
        'texture_size': texture_size,
        'world_area': round(area, 6),
        'uv_area_before': round(uv_area, 6),
        'uv_area_after': st.get('uv_area'),
        'scale_factor': round(factor, 4),
        'capped_by_uv_space': capped,
        'uv': st,
        'note': ('这个物体表面积 ' + str(round(area, 3)) + ' 平方单位，'
                 + '用 ' + str(texture_size) + 'px 贴图 + 密度 ' + str(density)
                 + ' 需要 UV 面积 ' + str(round(ideal_uv_area, 3))
                 + '，超过 1.0 上限，已收到 0.98。'
                 + '实际密度只有 ' + str(round(achieved, 1)) + '。'
                 + '想要更高密度就得用更大的贴图，或接受纹理平铺（tiling）。')
        if capped else
        ('目标密度 ' + str(density) + ' 已达成，UV 占用 '
         + str(round(st.get('uv_area', 0) * 100, 1)) + '%。'),
    }


def cmd_uv_inspect(params):
    """检查 UV 质量：有没有 UV、利用率、是否超出 0~1、密度是否合理。

    **在做贴图之前先查一次。** UV 有问题的话，贴图做得再好也是错的：
    超出 0~1 会平铺/裁切，利用率低会浪费分辨率，密度不一致会一精一糊。
    """
    name = params.get('object')
    if name:
        obj = _obj(name)
        if obj is None:
            raise ValueError('找不到对象：%s' % name)
        objs = [obj]
    else:
        objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']

    out = []
    for o in objs:
        st = _uv_stats(o)
        item = {'object': o.name, 'faces': len(o.data.polygons), **st}
        issues = []
        if not st.get('has_uv'):
            issues.append('没有 UV —— 无法用贴图，程序化纹理也只能靠 Object 坐标，'
                          '会导致所有材质密度不一致')
        else:
            util = st.get('utilization', 0)
            if util < 0.3:
                issues.append('UV 利用率只有 ' + str(round(util * 100, 1)) + '%，'
                              '贴图分辨率有很大浪费 —— 建议 uv_pack 重排')
            if st.get('outside_unit'):
                issues.append('UV 超出 0~1 范围 —— 会被平铺或裁切，通常不是想要的效果')
        item['issues'] = issues
        out.append(item)

    return {'objects': out, 'count': len(out)}


def cmd_uv_auto_all(params):
    """给整个场景（或指定对象）批量做 UV：unwrap + pack + 统一密度。

    用途：一次性把整个场景的 UV 都准备好，避免逐个处理。
    这是「渲染前必做」的一步——**没有 UV 就没有精确的贴图映射**。
    """
    size = int(params.get('texture_size', 1024))
    density = float(params.get('texel_density', 512.0))
    method = str(params.get('method', 'smart_project')).lower()
    margin = float(params.get('margin', 0.02))
    only_missing = bool(params.get('only_missing', False))

    targets = []
    if params.get('objects'):
        for n in params['objects']:
            o = bpy.data.objects.get(n)
            if o and o.type == 'MESH':
                targets.append(o)
    else:
        targets = [o for o in bpy.context.scene.objects if o.type == 'MESH']

    results = []
    ok = 0
    for o in targets:
        try:
            if only_missing and o.data.uv_layers:
                results.append({'object': o.name, 'skipped': '已有 UV'})
                continue
            if len(o.data.polygons) == 0:
                results.append({'object': o.name, 'skipped': '没有面'})
                continue

            # unwrap
            cmd_uv_unwrap({
                'object': o.name, 'method': method, 'island_margin': margin,
            })
            # 统一密度（不 pack，避免把密度重置掉）
            r = cmd_uv_scale({
                'object': o.name, 'texel_density': density, 'texture_size': size,
            })
            # pack 单独做：它只影响排布，不影响密度
            try:
                cmd_uv_pack({'object': o.name, 'margin': 0.003, 'rotate': True})
            except Exception:
                pass
            results.append({
                'object': o.name,
                'uv': r['uv'],
                'scale_factor': r['scale_factor'],
                'achieved_density': r.get('achieved_density'),
                'capped': r.get('capped_by_uv_space'),
            })
            ok += 1
        except Exception as e:
            results.append({'object': o.name, 'error': str(e)[:150]})

    return {
        'total': len(targets),
        'ok': ok,
        'results': results,
        'tip': '全部处理完。现在贴图密度在场景内一致了——'
               '同样的贴图尺寸下，大件和小件的细节密度接近。',
    }


# ══════════════════════════════════════════════════════ PBR 贴图制作 ══

def _noise_image(name, width, height, channels=4, fill=0.0):
    """新建一张空白图像（Blender 内部图像，可烘焙/写入再存盘）。

    注意：如果同名的图像已存在，Blender 会自动改名成 `name.001`，
    这会让调用方拿到的其实是**新图**，而代码里读的可能是旧图——
    重复烘焙时就会出现「第二次全是黑的」。
    所以这里先清掉同名旧图像。
    """
    old = bpy.data.images.get(name)
    if old is not None:
        try:
            bpy.data.images.remove(old)
        except Exception:
            pass
    # 也清掉所有 `name.NNN` 变体
    import re as _re
    pat = _re.compile(r'^' + _re.escape(name) + r'\.\d+$')
    for im in list(bpy.data.images):
        if pat.match(im.name):
            try:
                bpy.data.images.remove(im)
            except Exception:
                pass

    img = bpy.data.images.new(name=name, width=width, height=height, alpha=(channels == 4))
    if fill:
        px = [fill] * (width * height * 4)
        img.pixels.foreach_set(px)
    return img
    hdri = params.get('hdri_path')
    if hdri and os.path.isfile(hdri):
        # 有 HDRI 就用：这是最好的金属环境
        env = wnt.nodes.new('ShaderNodeTexEnvironment')
        env.location = (-200, 0)
        img = bpy.data.images.load(hdri, check_existing=True)
        env.image = img
        bg = wnt.nodes.new('ShaderNodeBackground')
        bg.location = (200, 0)
        bg.inputs['Strength'].default_value = float(params.get('world_strength', 1.0))
        wnt.links.new(env.outputs['Color'], bg.inputs['Color'])
        wnt.links.new(bg.outputs['Background'], out.inputs['Surface'])
        return {'mode': 'hdri', 'file': hdri, 'image': img.name}

    # 没有 HDRI：建程序化天空（渐变）
    # 用 Texture Coordinate 的 Generated Z 做上下渐变
    texco = wnt.nodes.new('ShaderNodeTexCoord')
    texco.location = (-800, 0)
    sep = wnt.nodes.new('ShaderNodeSeparateXYZ')
    sep.location = (-600, 0)
    wnt.links.new(texco.outputs['Generated'], sep.inputs['Vector'])

    ramp = wnt.nodes.new('ShaderNodeValToRGB')
    ramp.location = (-350, 0)
    # Generated 的 Z 在 0~1 范围（包围盒归一化），但世界坐标下用 Window 更合适
    # 这里用映射把 -1~1 变成 0~1
    mapn = wnt.nodes.new('ShaderNodeMapRange')
    mapn.location = (-480, 0)
    mapn.inputs['From Min'].default_value = -1.0
    mapn.inputs['From Max'].default_value = 1.0
    wnt.links.new(sep.outputs['Z'], mapn.inputs['Value'])
    wnt.links.new(mapn.outputs['Result'], ramp.inputs['Fac'])

    # 天空色：地平线偏暖暗，天顶偏冷亮
    world_color = _color4(params.get('world_color')) or (0.5, 0.55, 0.65, 1)
    zen_r = min(1.0, world_color[0] * 1.25)
    zen_g = min(1.0, world_color[1] * 1.25)
    zen_b = min(1.0, world_color[2] * 1.25)
    hor_r = world_color[0] * 0.35
    hor_g = world_color[1] * 0.35
    hor_b = world_color[2] * 0.35

    e = ramp.color_ramp.elements
    e[0].position = 0.0
    e[0].color = (hor_r, hor_g, hor_b, 1.0)
    e[1].position = 1.0
    e[1].color = (zen_r, zen_g, zen_b, 1.0)
    # 中间加一个地平线稍亮的带，模拟地面反光
    mid = e.new(0.5)
    mid.color = (world_color[0] * 0.8, world_color[1] * 0.8, world_color[2] * 0.8, 1.0)

    bg = wnt.nodes.new('ShaderNodeBackground')
    bg.location = (200, 0)
    bg.inputs['Strength'].default_value = float(params.get('world_strength', 1.0))
    wnt.links.new(ramp.outputs['Color'], bg.inputs['Color'])
    wnt.links.new(bg.outputs['Background'], out.inputs['Surface'])

    return {
        'mode': 'gradient-sky',
        'note': '没有 HDRI 时用程序化渐变天空。它能给出基本的明暗变化，'
                '让金属显出金属感；但真实感不如 HDRI。'
                '要更好的效果，给 hdri_path 传一个 .hdr/.exr 文件'
                '（Poly Haven 有免费 HDRI：https://polyhaven.com/hdris）。',
    }


def cmd_setup_render(params):
    """配置渲染引擎与光影参数。

    用途：材质做好后要渲染看效果。这个命令一次把引擎、采样、
    色彩管理、世界光、灯光都设好，避免逐项调。
    """
    scene = bpy.context.scene
    engine = params.get('engine', 'BLENDER_EEVEE')

    # 引擎名可能因版本不同，做个映射
    wanted = {
        'eevee': 'BLENDER_EEVEE',
        'eevee_next': 'BLENDER_EEVEE_NEXT',
        'cycles': 'CYCLES',
        'workbench': 'BLENDER_WORKBENCH',
    }.get(engine.lower(), engine)

    # 检查该引擎是否真的可用
    avail = []
    try:
        avail = [e.identifier for e in
                 bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items]
    except Exception:
        pass
    if avail and wanted not in avail:
        # 尝试回退
        for cand in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'CYCLES', 'BLENDER_WORKBENCH'):
            if cand in avail:
                wanted = cand
                break

    scene.render.engine = wanted
    actual = scene.render.engine

    if params.get('samples') is not None:
        try:
            if 'CYCLES' in actual:
                scene.cycles.samples = int(params['samples'])
            else:
                scene.eevee.taa_render_samples = int(params['samples'])
        except Exception:
            pass

    if params.get('resolution') is not None:
        r = params['resolution']
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            scene.render.resolution_x = int(r[0])
            scene.render.resolution_y = int(r[1])
    if params.get('resolution_percentage') is not None:
        scene.render.resolution_percentage = int(params['resolution_percentage'])

    # 色彩管理：Filmic / AgX 让高光不过曝
    view = params.get('view_transform')
    if view:
        try:
            scene.view_settings.view_transform = view
        except Exception:
            pass
    elif actual == 'CYCLES' or 'EEVEE' in actual:
        for cand in ('AgX', 'Filmic', 'Standard'):
            try:
                scene.view_settings.view_transform = cand
                break
            except Exception:
                continue

    # 世界光：金属渲染的关键，用渐变天空或 HDRI
    world_strength = float(params.get('world_strength', 0.6))
    world_info = _build_world_sky(scene, params)

    # 曝光补偿：这是控制整体亮度的**正确旋钮**。
    # 调灯光功率会让明暗关系变；调曝光只改整体亮度，不破坏光比。
    exposure = params.get('exposure')
    if exposure is not None:
        try:
            scene.view_settings.exposure = float(exposure)
        except Exception:
            pass

    # 自动曝光：如果调用方没指定曝光，根据世界光+灯光估算一个合理值。
    # 这避免「默认设置出来一片死白」——那是新手最常遇到的问题。
    if exposure is None and params.get('auto_exposure', True):
        # 经验：世界光 0.6 + 三点布光时曝光 0 刚好；
        # 世界光越强越要压曝光
        try:
            scene.view_settings.exposure = -0.9 * max(0.0, world_strength - 0.6)
        except Exception:
            pass

    # 三点布光（可选）。功率必须按场景尺度算，不能写死——
    # 一个 2 米的零件和一个 200 米的场景，同样 800W 的效果差两个数量级。
    lights = []
    if params.get('add_lights', True):
        # 清掉上次由本工具创建的灯
        for o in list(bpy.data.objects):
            if o.type == 'LIGHT' and o.name.startswith('__pbr_light'):
                bpy.data.objects.remove(o, do_unlink=True)

        # 算场景尺度：用可见 mesh 的包围盒对角线
        import mathutils
        pts = []
        for o in scene.objects:
            if o.type == 'MESH' and o.visible_get():
                try:
                    pts.extend([o.matrix_world @ mathutils.Vector(c) for c in o.bound_box])
                except Exception:
                    pass
        if pts:
            mn = mathutils.Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
            mx = mathutils.Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
            center = (mn + mx) / 2
            diag = (mx - mn).length or 1.0
        else:
            center = mathutils.Vector((0, 0, 0))
            diag = 4.0

        # 面光功率 ∝ 距离²。经验公式：energy ≈ (diag²) × 系数
        #
        # 关键：这个系数要配合世界光。如果世界光已经很亮，
        # 灯光再加很猛，结果一定过曝。所以按世界光强度反向缩放灯光。
        base = (diag ** 2) * 12.0
        # 世界光越强，灯越弱（保持总光量大致恒定）
        world_norm = max(0.25, min(2.5, 0.6 / max(0.15, world_strength)))
        base *= world_norm
        # 用户可整体缩放
        base *= float(params.get('light_scale', 1.0))

        d = diag * 0.9
        specs = [
            ('key', center + mathutils.Vector((d, -d, d * 0.9)), base * 1.0),
            ('fill', center + mathutils.Vector((-d, -d * 0.7, d * 0.4)), base * 0.35),
            ('rim', center + mathutils.Vector((0, d, d * 0.8)), base * 0.6),
        ]
        for nm, loc, energy in specs:
            ld = bpy.data.lights.new('__pbr_light_%s' % nm, type='AREA')
            ld.energy = max(1.0, energy)
            # 灯的尺寸也按场景缩放：小场景用小灯才有锐利高光
            ld.size = max(0.1, diag * 0.35)
            lo = bpy.data.objects.new('__pbr_light_%s' % nm, ld)
            lo.location = loc
            lo.rotation_euler = (center - loc).to_track_quat('-Z', 'Y').to_euler()
            bpy.context.scene.collection.objects.link(lo)
            lights.append({'name': lo.name, 'energy': round(ld.energy, 1), 'size': round(ld.size, 2)})

    return {
        'engine_requested': engine,
        'engine_actual': actual,
        'available_engines': avail,
        'view_transform': getattr(scene.view_settings, 'view_transform', None),
        'world': world_info,
        'world_strength': world_strength,
        'lights': lights,
        'resolution': [scene.render.resolution_x, scene.render.resolution_y],
    }


def cmd_render_preview(params):
    """渲染一张预览图（用于看材质与光影效果，不是四视图验收）。

    与 model_loop 的渲染不同：这是**自由视角、自由引擎**的效果图，
    给材质调整用的即时反馈。
    """
    scene = bpy.context.scene
    filepath = params.get('filepath')
    if not filepath:
        raise ValueError('必须给 filepath')

    import os
    d = os.path.dirname(filepath)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

    # 相机
    cam = None
    if params.get('camera'):
        cam = bpy.data.objects.get(params['camera'])
    if cam is None:
        cam = scene.camera

    # ── 什么时候才自动框选 ──
    #
    # 踩过的坑：这里原来是 `if cam is None or params.get('auto_frame', True):`
    # —— auto_frame **默认为 True**，于是**场景里明明有相机也会被重定位**。
    # 用户自己摆好的机位每次渲染都被冲掉，渲出来是一整张黑
    # （自动框选的是全场景，而用户想拍的是某个局部）。
    #
    # 正确逻辑：
    #   · 场景**有**相机 → **直接用**，不动它（尊重用户/模型摆的机位）
    #   · 场景**没**相机 → 自动建一个能看全场景的（这时只能这么做）
    #   · 显式传 auto_frame: true → 才强制重新框选（想自动构图时用）
    auto_frame = params.get('auto_frame')
    need_auto = (cam is None) or (auto_frame is True)
    if need_auto:
        import mathutils
        cd = bpy.data.cameras.new('__pbr_cam') if cam is None else cam.data
        if cam is None:
            cam = bpy.data.objects.new('__pbr_cam', cd)
            scene.collection.objects.link(cam)
        cd.lens = 50
        # 算场景包围盒
        pts = []
        for o in scene.objects:
            if o.type == 'MESH' and o.visible_get():
                try:
                    pts.extend([o.matrix_world @ mathutils.Vector(c) for c in o.bound_box])
                except Exception:
                    pass
        if pts:
            mn = mathutils.Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
            mx = mathutils.Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
            center = (mn + mx) / 2
            radius = max((p - center).length for p in pts) or 1.0
            # 45° 俯视，距离按半径和视场角算，保证装得下。
            # 系数 1.25 是留边距——1.0 会刚好贴边，视觉上很挤。
            import math
            dist = radius / math.tan(math.radians(22)) * 1.25
            az = math.radians(-55)
            el = math.radians(24)
            cam.location = center + mathutils.Vector((
                dist * math.cos(el) * math.cos(az),
                dist * math.cos(el) * math.sin(az),
                dist * math.sin(el),
            ))
            direction = center - cam.location
            cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
            # 裁切面跟着尺度走，避免小物体被裁掉或大物体被切
            cd.clip_start = max(0.001, radius * 0.01)
            cd.clip_end = max(10.0, radius * 100)
        scene.camera = cam

    scene.render.filepath = filepath
    scene.render.image_settings.file_format = 'PNG'
    if params.get('width'):
        scene.render.resolution_x = int(params['width'])
    if params.get('height'):
        scene.render.resolution_y = int(params['height'])
    if params.get('samples') is not None:
        try:
            if scene.render.engine == 'CYCLES':
                scene.cycles.samples = int(params['samples'])
            else:
                scene.eevee.taa_render_samples = int(params['samples'])
        except Exception:
            pass

    _ops(bpy.ops.render.render, write_still=True)

    return {
        'filepath': scene.render.filepath,
        'engine': scene.render.engine,
        'resolution': [scene.render.resolution_x, scene.render.resolution_y],
    }


# ══════════════════════════════════════════════════════ 对象管理 ══

def cmd_select_objects(params):
    names = params.get('names') or []
    if params.get('deselect_others', True):
        _deselect_all()
    for n in names:
        o = bpy.data.objects.get(n)
        if o:
            o.select_set(True)
    if params.get('active'):
        a = bpy.data.objects.get(params['active'])
        if a:
            bpy.context.view_layer.objects.active = a
    elif names:
        first = bpy.data.objects.get(names[0])
        if first:
            bpy.context.view_layer.objects.active = first
    return {'selected': [o.name for o in bpy.context.scene.objects if o.select_get()],
            'active': bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None}


def cmd_delete_objects(params):
    """删除对象，并在删除**之前**留下可复原的快照。

    为什么要在删除前抓：删完位置信息就没了，只剩一个名字——
    之后想知道「这东西原来在哪儿、多大、属于哪个组」全都拿不到，
    也就没法判断「该不该加回、加回到哪」。

    快照包含：世界坐标包围盒、中心点、尺寸、三角面数、材质、
    所属集合、父子关系。有了这些，复原时可以直接用数值放回去。

    返回里带 `snapshots`，上层（插件）把它记进删除账本，
    之后要求模型显式交代每一个的去留。
    """
    from mathutils import Vector
    names = params.get('names') or []
    deleted = []
    snapshots = []

    for n in names:
        o = bpy.data.objects.get(n)
        if o is None:
            continue
        # ── 删除前抓快照 ──
        try:
            snap = {
                'name': o.name,
                'type': o.type,
                'location': [round(v, 6) for v in o.location],
                'rotation': [round(v, 6) for v in o.rotation_euler],
                'scale': [round(v, 6) for v in o.scale],
                'dimensions': [round(v, 6) for v in o.dimensions],
                'parent': o.parent.name if o.parent else None,
                'collections': [c.name for c in o.users_collection],
                'materials': [s.material.name for s in o.material_slots if s.material],
                'custom_props': {k: str(v)[:120] for k, v in o.items() if not k.startswith('_')},
            }
            if o.type == 'MESH' and o.data:
                snap['vertices'] = len(o.data.vertices)
                snap['triangles'] = sum(len(p.vertices) - 2 for p in o.data.polygons)
                # 世界坐标包围盒：复原时用它对齐
                try:
                    pts = [o.matrix_world @ Vector(c) for c in o.bound_box]
                    mn = [min(p[i] for p in pts) for i in range(3)]
                    mx = [max(p[i] for p in pts) for i in range(3)]
                    snap['bbox'] = [round(v, 6) for v in (mn + mx)]
                    snap['center'] = [round((mn[i] + mx[i]) / 2, 6) for i in range(3)]
                except Exception:
                    pass
            snapshots.append(snap)
        except Exception as e:
            snapshots.append({'name': o.name, 'error': str(e)[:120]})

        bpy.data.objects.remove(o, do_unlink=not params.get('keep_mesh_data', False))
        deleted.append(n)

    out = {'deleted': deleted, 'missing': [n for n in names if n not in deleted]}
    if snapshots:
        out['snapshots'] = snapshots
        out['note'] = ('已记录 %d 个对象的删除前快照（位置/尺寸/材质/所属组）。'
                       '之后必须交代每一个的去留：确实多余，还是误删要加回；'
                       '要加回的按快照里的坐标放回去。' % len(snapshots))
    return out


def cmd_rename_object(params):
    o = _obj(params['name'])
    old = o.name
    o.name = params['new_name']
    if params.get('set_custom_prop', True):
        o['ptc_part'] = params['new_name']
    return {'old': old, 'new': o.name}


def cmd_join_objects(params):
    names = params.get('names') or []
    if len(names) < 2:
        raise ValueError('至少需要两个对象')
    objs = [_obj(n) for n in names]
    _deselect_all()
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    _ops(bpy.ops.object.join)
    result = _active()
    if params.get('result_name'):
        result.name = params['result_name']
    return {'name': result.name, 'merged': names, 'faces': len(result.data.polygons)}


def cmd_separate_object(params):
    o = _obj(params['name'])
    mode = params.get('mode', 'LOOSE')
    before = {x.name for x in bpy.context.scene.objects}
    _activate(o)
    _ops(bpy.ops.object.mode_set, mode='EDIT')
    try:
        _ops(bpy.ops.mesh.select_all, action='SELECT')
        if mode == 'MATERIAL':
            _ops(bpy.ops.mesh.separate, type='MATERIAL')
        else:
            _ops(bpy.ops.mesh.separate, type='LOOSE')
    finally:
        _ops(bpy.ops.object.mode_set, mode='OBJECT')
    after = {x.name for x in bpy.context.scene.objects}
    return {'source': o.name, 'mode': mode, 'created': sorted(after - before)}


def cmd_set_origin(params):
    o = _obj(params['name'])
    mode = params.get('mode', 'GEOMETRY')
    _activate(o)
    try:
        if mode == 'CURSOR':
            _ops(bpy.ops.object.origin_set, type='ORIGIN_CURSOR')
        elif mode == 'CENTER_OF_MASS':
            _ops(bpy.ops.object.origin_set, type='ORIGIN_CENTER_OF_MASS')
        elif mode == 'BOUNDS':
            _ops(bpy.ops.object.origin_set, type='ORIGIN_GEOMETRY', center='BOUNDS')
        else:
            _ops(bpy.ops.object.origin_set, type='ORIGIN_GEOMETRY', center='MEDIAN')
    except Exception as e:
        raise ValueError('设置原点失败：%s' % e)
    return {'name': o.name, 'mode': mode,
            'location': [round(v, 6) for v in o.location]}


# ══════════════════════════════════════════════════════ 观察渲染 ══

VIEW_DIRS = {
    'front': (0, -1, 0),
    'back': (0, 1, 0),
    'left': (-1, 0, 0),
    'right': (1, 0, 0),
    'top': (0, 0, 1),
    'bottom': (0, 0, -1),
    'iso': (1, -1, 0.8),
}

# 上一次渲染的空图警告（_render_to_png 只返回字节，用这个变量把警告带出去）
_LAST_RENDER_WARNING = None


def _scene_bounds(objs):
    from mathutils import Vector
    mn = Vector((1e18, 1e18, 1e18))
    mx = Vector((-1e18, -1e18, -1e18))
    found = False
    for o in objs:
        if o.type != 'MESH':
            continue
        found = True
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            for i in range(3):
                mn[i] = min(mn[i], w[i])
                mx[i] = max(mx[i], w[i])
    if not found:
        return Vector((0, 0, 0)), Vector((1, 1, 1))
    return mn, mx


def _png_looks_blank(data):
    """粗检一张 PNG 是不是「几乎全是同一个颜色」（即画面上没有物体）。

    **不要去猜背景色的具体 RGB 值。**
    踩过的坑：我一开始假设背景是线性值 (0.08,0.09,0.11) 对应的 sRGB
    (24,25,28)，结果实际渲染出来是 (76,83,92)——Blender 的颜色管理
    会把线性值转成 sRGB，转换关系不是简单的 0.08×255。
    按错误的值去比对，空图反而判成"有内容"。

    可靠的判据是**颜色一致性**：空图的所有像素都是同一个背景色，
    方差极小。有物体的图一定有对比（物体比背景亮或暗）。
    所以改成：
      ① 统计出现最多的颜色，算它占的比例
      ② 比例极高（> 99%）→ 判定为空图
      ③ 同时要求该颜色的方差极小，避免把"纯色物体铺满画面"误判

    返回 None 表示没问题；返回一句说明表示疑似空图。
    """
    try:
        import struct
        import zlib

        if data[:8] != b'\x89PNG\r\n\x1a\n':
            return None

        pos = 8
        width = height = bit_depth = color_type = None
        idat = []
        while pos + 8 <= len(data):
            (length,) = struct.unpack('>I', data[pos:pos + 4])
            ctype = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + length]
            if ctype == b'IHDR':
                width, height, bit_depth, color_type = struct.unpack('>IIBB', body[:10])
            elif ctype == b'IDAT':
                idat.append(body)
            elif ctype == b'IEND':
                break
            pos += 12 + length

        if not width or not height or bit_depth != 8:
            return None
        if color_type not in (0, 2, 6):
            return None
        channels = {0: 1, 2: 3, 6: 4}[color_type]

        raw = zlib.decompress(b''.join(idat))
        stride = width * channels
        sample_rows = min(height, 240)
        prev = bytearray(stride)

        # 量化到 8 级/通道做直方图（抗轻微噪声）
        hist = {}
        total = 0
        off = 0
        for y in range(sample_rows):
            if off >= len(raw):
                break
            filt = raw[off]
            off += 1
            line = bytearray(raw[off:off + stride])
            off += stride
            if filt == 1:
                for i in range(channels, stride):
                    line[i] = (line[i] + line[i - channels]) & 0xFF
            elif filt == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif filt == 3:
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif filt == 4:
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i - channels] if i >= channels else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            prev = line
            for x in range(0, stride, channels):
                r = line[x]
                g = line[x + 1] if channels >= 3 else line[x]
                b = line[x + 2] if channels >= 3 else line[x]
                # 量化到 16 级（每级 16），吸收轻微噪声
                key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
                hist[key] = hist.get(key, 0) + 1
                total += 1

        if total < 100:
            return None
        top = max(hist.values())
        ratio = top / total
        # 空图：单一颜色占 99.2% 以上
        if ratio > 0.992:
            return ('渲染结果几乎全是同一种颜色（%.1f%% 的像素相同）——'
                    '画面上很可能没有任何物体，或相机没框到物体。'
                    % (ratio * 100))
        return None
    except Exception:
        return None


def _pick_render_engine(params):
    """选一个能出图的渲染引擎。

    为什么不用 Workbench：Workbench **不吃场景灯、也不吃材质色**。
    「打灯 + 看图」这条路必须走真引擎（EEVEE / Cycles）。

    EEVEE 优先：实测同样场景 458ms，Cycles 要 2100ms。
    如果调用方显式指定了 engine（且不是 Workbench），就听它的。
    """
    want = (params.get('engine') or '').upper()
    if 'EEVEE' in want:
        return 'BLENDER_EEVEE_NEXT' if _has_engine('BLENDER_EEVEE_NEXT') else 'BLENDER_EEVEE'
    if 'CYCLES' in want:
        return 'CYCLES'
    # 默认 EEVEE
    return 'BLENDER_EEVEE_NEXT' if _has_engine('BLENDER_EEVEE_NEXT') else 'BLENDER_EEVEE'


def _has_engine(name):
    try:
        items = bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items
        return any(it.identifier == name for it in items)
    except Exception:
        return False


def _ensure_viewable(sc):
    """让真引擎渲染不至于全黑：没灯就补一盏，没世界就补一个。

    为什么需要它：EEVEE 在**没有灯光**的场景里渲染结果是纯黑。
    旧实现用「一律走 Workbench」来回避这个问题，代价是永远看不到真实光照。
    正确做法是**补一个可用的照明**，而不是放弃能力。

    只在真的没有灯/没有世界时才补，而且只补临时对象，调用方在 finally 里清掉。
    返回原环境与临时数据记录，供 _restore_viewable 在 finally 中恢复。
    """
    created = {'objects': [], 'lights': [], 'world': sc.world, 'temporary_world': None}
    try:
        has_light = any(o.type == 'LIGHT' and not o.hide_render
                        for o in sc.objects)
        if not has_light:
            ld = bpy.data.lights.new('__mcp_fill', type='SUN')
            created['lights'].append(ld)
            ld.energy = 3.0
            # 从侧上方打，避免正对镜头导致平面全亮无层次
            li = bpy.data.objects.new('__mcp_fill', ld)
            created['objects'].append(li)
            sc.collection.objects.link(li)
            li.rotation_euler = (0.9, 0.15, 0.6)
        # 只在临时世界副本上补光，保留原世界的节点、强度和共享关系。
        w = sc.world
        bg = w.node_tree.nodes.get('Background') if w and w.use_nodes else None
        needs_world = w is None or not w.use_nodes or (
            bg is not None and not bg.inputs[1].is_linked and bg.inputs[1].default_value < 0.05)
        if needs_world:
            w = w.copy() if w is not None else bpy.data.worlds.new('__mcp_world')
            created['temporary_world'] = w
            sc.world = w
            w.use_nodes = True
            bg = w.node_tree.nodes.get('Background')
            if bg is not None and not bg.inputs[1].is_linked and bg.inputs[1].default_value < 0.05:
                bg.inputs[1].default_value = 1.0
    except Exception:
        pass
    return created


def _restore_viewable(sc, created):
    """成功或渲染失败均恢复环境，并清理临时补光数据块。"""
    if not created:
        return
    sc.world = created['world']
    for collection, items in ((bpy.data.objects, created['objects']),
                              (bpy.data.lights, created['lights']),
                              (bpy.data.worlds, [created['temporary_world']])):
        for item in items:
            if item is not None:
                try:
                    collection.remove(item, do_unlink=True)
                except Exception:
                    pass


def _render_to_png(params, cam_loc, cam_target, lens=50.0, ortho=False, ortho_scale=None,
                   objs=None):
    """临时建相机渲染一张，返回 PNG 字节。

    空图检测的结果通过模块级 _LAST_RENDER_WARNING 传出去，
    所以这里必须声明 global——**不加的话赋值只作用于局部变量**，
    模块级永远是 None，调用方读到的永远是空的（踩过这个坑）。
    """
    global _LAST_RENDER_WARNING
    sc = bpy.context.scene
    from mathutils import Vector

    saved = {
        'engine': sc.render.engine,
        'res_x': sc.render.resolution_x,
        'res_y': sc.render.resolution_y,
        'pct': sc.render.resolution_percentage,
        'camera': sc.camera,
        'filepath': sc.render.filepath,
        'fmt': sc.render.image_settings.file_format,
        # 着色设置也要存——它是每个视角现改的，不恢复会污染下一次渲染
        'color_type': None,
        'single_color': None,
        'background_type': None,
        'background_color': None,
        'show_cavity': None,
        'shading_type': sc.display.shading.type,
        'shading_light': sc.display.shading.light,
    }
    try:
        saved['color_type'] = sc.display.shading.color_type
        saved['single_color'] = tuple(sc.display.shading.single_color)
        saved['background_type'] = sc.display.shading.background_type
        saved['background_color'] = tuple(sc.display.shading.background_color)
        saved['show_cavity'] = sc.display.shading.show_cavity
    except Exception:
        pass

    cam_data = bpy.data.cameras.new('__mcp_cam')
    cam_data.lens = lens
    cam_data.type = 'ORTHO' if ortho else 'PERSP'
    if ortho and ortho_scale:
        cam_data.ortho_scale = ortho_scale
    cam_obj = bpy.data.objects.new('__mcp_cam', cam_data)
    sc.collection.objects.link(cam_obj)

    created_fill = None
    try:
        cam_obj.location = Vector(cam_loc)
        direction = Vector(cam_target) - Vector(cam_loc)
        if direction.length < 1e-6:
            direction = Vector((0, 0, -1))
        cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
        sc.camera = cam_obj

        sc.render.resolution_x = int(params.get('width') or 800)
        sc.render.resolution_y = int(params.get('height') or 600)
        sc.render.resolution_percentage = 100
        sc.render.image_settings.file_format = 'PNG'

        shading = params.get('shading', 'solid')

        # ── 渲染引擎的选择 ──
        #
        # 这里**曾经一律用 Workbench**，注释写的是「EEVEE 在无灯光时会全黑」。
        # 那个担心是对的，但代价是：Workbench **既不吃场景灯、也不吃材质色**。
        # 实测（开灯/关灯 × solid/material/rendered 共 6 种组合）哈希完全相同；
        # 把材质改成品红，图仍然是灰白。也就是说 shading='rendered' 这个参数
        # **名不副实**——模型以为自己在看光照效果，实际看的是固定 studio 光。
        #
        # 现在按 shading 分派：
        #   rendered → 真引擎（EEVEE）+ 场景灯 + 材质，看的是真实光照效果
        #   其它     → Workbench，看形体最快最稳
        #
        # 「无灯光会全黑」这个风险用**自动补光**兜住（见下面 _ensure_viewable），
        # 而不是靠牺牲能力来回避。
        want_rendered = shading == 'rendered'
        if want_rendered:
            sc.render.engine = _pick_render_engine(params)
            sc.display.shading.type = 'RENDERED'
            # 没灯就临时补一盏，否则真引擎渲染出来是纯黑
            created_fill = _ensure_viewable(sc)
        else:
            sc.render.engine = 'BLENDER_WORKBENCH'
        try:
            sc.display.shading.light = 'STUDIO'
            sc.display.shading.show_cavity = True
            sc.display.shading.background_type = 'VIEWPORT'
            sc.display.shading.background_color = (0.08, 0.09, 0.11)

            # **着色模式必须根据「有没有材质」决定**，不能一律用 MATERIAL。
            #
            # 踩过的坑：这里原来写成
            #   color_type = 'MATERIAL' if shading in ('material','rendered') else 'MATERIAL'
            # 三元两边都是 MATERIAL（等于没有分支）。而**没赋材质的物体在 MATERIAL
            # 模式下渲染出来是背景色**——看起来"出图成功"，实际是一张空图。
            # 模型拿到空图会得出完全错误的判断（以为结构有问题、或者以为没问题）。
            #
            # 正确做法：
            #   - 想要材质色，但**场景里真的没有材质** → 回退到 OBJECT 模式
            #     （OBJECT 模式按物体色渲染，没材质也可见）
            #   - 其它情况（solid / wireframe / xray）→ SINGLE 模式，
            #     统一用一个中灰，形体最清楚
            want_material = shading in ('material', 'rendered')
            has_any_material = any(
                s.material for o in (objs or [])
                if getattr(o, 'type', '') == 'MESH'
                for s in getattr(o, 'material_slots', [])
            ) if objs else bool(len(bpy.data.materials))

            if want_material and has_any_material:
                sc.display.shading.color_type = 'MATERIAL'
            elif want_material and not has_any_material:
                # 要材质色但没材质 → 用 OBJECT 模式回退，保证可见
                sc.display.shading.color_type = 'OBJECT'
                sc.display.shading.object_color = (0.62, 0.62, 0.64, 1.0)
            else:
                # 形体模式：统一中灰，最能看清轮廓
                sc.display.shading.color_type = 'SINGLE'
                sc.display.shading.single_color = (0.62, 0.62, 0.64)
        except Exception:
            pass

        fd, path = tempfile.mkstemp(suffix='.png', prefix='mcp_')
        os.close(fd)
        sc.render.filepath = path
        _ops(bpy.ops.render.render, write_still=True)

        with open(path, 'rb') as f:
            data = f.read()

        # ── 空图检测 ──
        # 踩过的坑：着色模式错用时渲染出来是**纯背景色**，
        # 但工具照样报告"出图成功"，模型拿到空图会做出完全错误的判断
        # （以为结构有问题，或者以为一切正常）。
        # 这里做一次粗检：把 PNG 解出像素，看有没有明显不是背景的像素。
        # 检测失败**不阻断**渲染（宁可能出图），只在返回值里附一个警告标记。
        blank = None
        try:
            blank = _png_looks_blank(data)
        except Exception:
            blank = None

        try:
            os.unlink(path)
        except Exception:
            pass
        if blank:
            # 把警告挂在 params 上，由上层读走（_render_to_png 只返回字节，
            # 所以用一个模块级变量传递——调用方是同一个线程，安全）
            _LAST_RENDER_WARNING = blank
        else:
            _LAST_RENDER_WARNING = None
        return data
    finally:
        try:
            sc.render.engine = saved['engine']
            sc.render.resolution_x = saved['res_x']
            sc.render.resolution_y = saved['res_y']
            sc.render.resolution_percentage = saved['pct']
            sc.render.filepath = saved['filepath']
            sc.render.image_settings.file_format = saved['fmt']
            sc.camera = saved['camera']
            bpy.data.objects.remove(cam_obj, do_unlink=True)
            bpy.data.cameras.remove(cam_data)
        except Exception:
            pass
        # 临时补的光也要清掉 —— 它是为了「不让真引擎出全黑图」而加的，
        # 不能留在场景里污染模型自己的布光。
        try:
            _restore_viewable(sc, created_fill)
        except Exception:
            pass
        # 着色设置也恢复：每个视角都会现改它，不恢复会污染后续渲染
        # （踩过的坑：模型用 material 模式看完后，下一轮 solid 渲染仍用旧设置）
        try:
            sc.display.shading.type = saved['shading_type']
            sc.display.shading.light = saved['shading_light']
            if saved['color_type'] is not None:
                sc.display.shading.color_type = saved['color_type']
            if saved['single_color'] is not None:
                sc.display.shading.single_color = saved['single_color']
            if saved['background_type'] is not None:
                sc.display.shading.background_type = saved['background_type']
            if saved['background_color'] is not None:
                sc.display.shading.background_color = saved['background_color']
            if saved['show_cavity'] is not None:
                sc.display.shading.show_cavity = saved['show_cavity']
        except Exception:
            pass


def cmd_render_view(params):
    from mathutils import Vector
    view = params.get('view', 'viewport')
    focus = params.get('focus')
    hide_others = params.get('hide_others', False)

    if focus:
        objs = [bpy.data.objects[n] for n in focus if n in bpy.data.objects]
    else:
        objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']

    mn, mx = _scene_bounds(objs)
    center = (mn + mx) / 2
    size = max((mx - mn).length / 2, 1e-3)
    dist = size * 2.8

    hidden_saved = []
    if hide_others and focus:
        keep = set(focus)
        for o in bpy.context.scene.objects:
            if o.type == 'MESH' and o.name not in keep:
                hidden_saved.append((o.name, o.hide_render))
                o.hide_render = True

    try:
        if view == 'angles':
            dirs = [('front', (0, -1, 0)), ('right', (1, 0, 0)), ('iso', (1, -1, 0.8))]
            # 拼成一张：分别渲染后纵向拼接
            images = []
            for name, d in dirs:
                v = Vector(d).normalized()
                loc = center + v * dist
                images.append((name, _render_to_png(params, loc, center, ortho=True,
                                                    ortho_scale=size * 2.2, objs=objs)))
            png = _vertically_concat_png(images, int(params.get('width') or 800))
            return {
                'summary': '多角度拼图（上到下：%s）' % '、'.join(n for n, _ in images),
                'image_base64': base64.b64encode(png).decode('ascii'),
                'views': [n for n, _ in images],
            }

        if view == 'camera' and bpy.context.scene.camera:
            cam = bpy.context.scene.camera
            png = _render_to_png(params, cam.matrix_world.translation, center, objs=objs)
            return {'summary': '场景相机视角', 'image_base64': base64.b64encode(png).decode('ascii')}

        d = VIEW_DIRS.get(view)
        if d is None:
            d = VIEW_DIRS['iso']
        v = Vector(d).normalized()
        loc = center + v * dist
        png = _render_to_png(params, loc, center, ortho=(view != 'viewport'),
                             ortho_scale=size * 2.2, objs=objs)
        out = {
            'summary': '视角 %s · %dx%d · 中心 %s' % (
                view, int(params.get('width') or 800), int(params.get('height') or 600),
                [round(x, 3) for x in center]),
            'image_base64': base64.b64encode(png).decode('ascii'),
            'view': view,
        }
        # 空图警告：图片看着"出图成功"但其实是纯背景时，必须明确说出来。
        # 不说的话模型会基于空图判断，得出完全错误的结论。
        if _LAST_RENDER_WARNING:
            out['warning'] = _LAST_RENDER_WARNING
            out['summary'] += ' ⚠ ' + _LAST_RENDER_WARNING
        return out
    finally:
        for name, hide in hidden_saved:
            o = bpy.data.objects.get(name)
            if o:
                o.hide_render = hide


def _vertically_concat_png(images, width):
    """把多张 PNG 纵向拼起来。用 Blender 自带的图像 API，避免依赖 PIL。"""
    loaded = []
    try:
        for name, data in images:
            fd, path = tempfile.mkstemp(suffix='.png', prefix='mcp_cat_')
            os.close(fd)
            with open(path, 'wb') as f:
                f.write(data)
            img = bpy.data.images.load(path)
            loaded.append((img, path))

        h_each = loaded[0][0].size[1]
        total_h = h_each * len(loaded)
        w = loaded[0][0].size[0]

        out = bpy.data.images.new('__mcp_concat', width=w, height=total_h, alpha=False)
        buf = [0.0] * (w * total_h * 4)
        for idx, (img, _) in enumerate(loaded):
            px = list(img.pixels)
            row_offset = (len(loaded) - 1 - idx) * h_each  # 第一张放最上面
            for y in range(h_each):
                src = y * w * 4
                dst = ((row_offset + y) * w) * 4
                buf[dst:dst + w * 4] = px[src:src + w * 4]
        out.pixels = buf

        fd, outpath = tempfile.mkstemp(suffix='.png', prefix='mcp_out_')
        os.close(fd)
        out.filepath_raw = outpath
        out.file_format = 'PNG'
        out.save()
        with open(outpath, 'rb') as f:
            data = f.read()
        try:
            os.unlink(outpath)
        except Exception:
            pass
        bpy.data.images.remove(out)
        return data
    finally:
        for img, path in loaded:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass
            try:
                os.unlink(path)
            except Exception:
                pass


def cmd_render_image(params):
    sc = bpy.context.scene
    saved = {
        'engine': sc.render.engine,
        'res_x': sc.render.resolution_x,
        'res_y': sc.render.resolution_y,
        'pct': sc.render.resolution_percentage,
        'filepath': sc.render.filepath,
        'fmt': sc.render.image_settings.file_format,
        'samples': getattr(sc, 'eevee', None) and getattr(sc.eevee, 'taa_render_samples', None),
    }
    try:
        if params.get('engine'):
            try:
                sc.render.engine = params['engine']
            except Exception:
                pass
        sc.render.resolution_x = int(params.get('resolution_x') or 1920)
        sc.render.resolution_y = int(params.get('resolution_y') or 1080)
        sc.render.resolution_percentage = 100
        sc.render.image_settings.file_format = 'PNG'
        if params.get('samples'):
            try:
                sc.eevee.taa_render_samples = int(params['samples'])
            except Exception:
                pass

        target = params.get('filepath')
        tmp = None
        if not target:
            fd, tmp = tempfile.mkstemp(suffix='.png', prefix='mcp_render_')
            os.close(fd)
            target = tmp
        sc.render.filepath = target
        _ops(bpy.ops.render.render, write_still=True)

        with open(target, 'rb') as f:
            data = f.read()
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass

        return {
            'summary': '渲染完成 %dx%d · %s · %d 字节%s' % (
                sc.render.resolution_x, sc.render.resolution_y, sc.render.engine,
                len(data), (' · 已存到 ' + params['filepath']) if params.get('filepath') else ''),
            'image_base64': base64.b64encode(data).decode('ascii'),
            'filepath': params.get('filepath'),
        }
    finally:
        try:
            sc.render.engine = saved['engine']
            sc.render.resolution_x = saved['res_x']
            sc.render.resolution_y = saved['res_y']
            sc.render.resolution_percentage = saved['pct']
            sc.render.filepath = saved['filepath']
            sc.render.image_settings.file_format = saved['fmt']
        except Exception:
            pass


def cmd_set_camera(params):
    sc = bpy.context.scene
    name = params.get('name')
    cam = None
    if name:
        cam = bpy.data.objects.get(name)
    if cam is None:
        cam = sc.camera
    if cam is None or cam.type != 'CAMERA':
        cam_data = bpy.data.cameras.new('Camera')
        cam = bpy.data.objects.new('Camera', cam_data)
        sc.collection.objects.link(cam)
        sc.camera = cam

    from mathutils import Vector
    if params.get('location'):
        cam.location = _vec(params['location'])
    if params.get('look_at'):
        target = Vector(_vec(params['look_at']))
        direction = target - cam.location
        if direction.length > 1e-6:
            cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    if params.get('lens') is not None:
        cam.data.lens = float(params['lens'])
    if params.get('ortho') is not None:
        cam.data.type = 'ORTHO' if params['ortho'] else 'PERSP'
    if params.get('ortho_scale') is not None:
        cam.data.ortho_scale = float(params['ortho_scale'])

    sc.camera = cam
    return {'camera': cam.name, 'location': [round(v, 4) for v in cam.location],
            'lens': cam.data.lens, 'type': cam.data.type}


def cmd_set_viewport_shading(params):
    sc = bpy.context.scene
    shading = params.get('shading', 'solid')
    try:
        sc.display.shading.type = shading.upper() if shading != 'rendered' else 'RENDERED'
    except Exception:
        pass
    if params.get('show_overlays') is not None:
        try:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.overlay.show_overlays = bool(params['show_overlays'])
        except Exception:
            pass
    if params.get('background'):
        try:
            sc.display.shading.background_type = params['background']
        except Exception:
            pass
    return {'shading': shading, 'background': params.get('background')}


# ══════════════════════════════════════════════════════ 导入导出 ══

def cmd_import_file(params):
    path = params['filepath']
    if not os.path.exists(path):
        raise ValueError('文件不存在：%s' % path)
    fmt = (params.get('format') or os.path.splitext(path)[1].lstrip('.')).lower()

    before = {o.name for o in bpy.context.scene.objects}
    if fmt in ('glb', 'gltf'):
        _ops(bpy.ops.import_scene.gltf, filepath=path)
    elif fmt == 'fbx':
        _ops(bpy.ops.import_scene.fbx, filepath=path)
    elif fmt == 'obj':
        try:
            _ops(bpy.ops.wm.obj_import, filepath=path)
        except AttributeError:
            _ops(bpy.ops.import_scene.obj, filepath=path)
    elif fmt == 'stl':
        try:
            _ops(bpy.ops.wm.stl_import, filepath=path)
        except AttributeError:
            _ops(bpy.ops.import_mesh.stl, filepath=path)
    elif fmt in ('usd', 'usda', 'usdc'):
        _ops(bpy.ops.wm.usd_import, filepath=path)
    elif fmt == 'blend':
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for o in dst.objects:
            if o:
                bpy.context.scene.collection.objects.link(o)
    else:
        raise ValueError('不支持的格式：%s' % fmt)

    after = {o.name for o in bpy.context.scene.objects}
    created = sorted(after - before)

    if params.get('scale') and created:
        s = float(params['scale'])
        for n in created:
            o = bpy.data.objects.get(n)
            if o:
                o.scale = [v * s for v in o.scale]

    return {'imported': created, 'count': len(created), 'format': fmt}


def _export_with_window_fallback(op, kwargs, path=None, selected_only=True, apply_mods=True):
    """导出 glTF。

    glTF 导出器的 operator 会调 context.window.cursor_set()，后台模式没有
    window 就抛 AttributeError。而且 Blender 拒绝用替身对象做 temp_override
    （要求真正的 bpy.types.Window）。

    所以后台模式下绕过 operator，直接调导出模块的 API。
    """
    if not bpy.app.background:
        return _ops(op, **kwargs)

    # 后台模式：直接调 io_scene_gltf2 的导出函数
    try:
        from io_scene_gltf2 import ExportGLTF2
        exporter = ExportGLTF2(
            bpy.context,
            export_format='GLB' if (path or '').lower().endswith('.glb') else 'GLTF_SEPARATE',
            filepath=path,
            use_selection=selected_only,
            export_apply=apply_mods,
        )
        return exporter.execute()
    except Exception as first_error:
        # 退回 operator，至少让错误可读
        try:
            return _ops(op, **kwargs)
        except Exception:
            raise RuntimeError(
                'glTF 导出失败（后台模式下 glTF 导出器需要窗口对象）。'
                '原始错误：%s。'
                '建议：用带界面的 Blender 运行 addon，或改用 OBJ/FBX/STL 格式导出。'
                % str(first_error)[:300])


def _export_obj_direct(path, selected_only=True, apply_mods=True):
    """后台模式下直接写 OBJ 文件。

    bpy.ops.wm.obj_export 的 poll() 在无界面模式下失败（需要窗口上下文），
    所以自己遍历网格写文件。OBJ 格式足够简单，手写反而更可控。
    """
    from mathutils import Vector

    objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if selected_only:
        sel = [o for o in objs if o.select_get()]
        if sel:
            objs = sel

    lines = ['# exported by blender-mcp']
    v_offset = 1
    for o in objs:
        # 应用修改器后的网格
        if apply_mods and o.modifiers:
            dg = bpy.context.evaluated_depsgraph_get()
            eo = o.evaluated_get(dg)
            me = eo.to_mesh()
        else:
            me = o.data

        try:
            mw = o.matrix_world
            lines.append('o %s' % o.name.replace(' ', '_'))
            for v in me.vertices:
                w = mw @ v.co
                lines.append('v %.6f %.6f %.6f' % (w.x, w.y, w.z))
            for p in me.polygons:
                idx = [v_offset + i for i in p.vertices]
                lines.append('f ' + ' '.join(str(i) for i in idx))
            v_offset += len(me.vertices)
        finally:
            if apply_mods and o.modifiers:
                try:
                    o.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
                except Exception:
                    pass

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def cmd_export_file(params):
    path = params['filepath']
    fmt = (params.get('format') or os.path.splitext(path)[1].lstrip('.')).lower()
    selected_only = params.get('selected_only', True)
    apply_mods = params.get('apply_modifiers', True)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)

    kwargs = {'filepath': path}
    if selected_only:
        kwargs['use_selection'] = True
    if apply_mods:
        kwargs['export_apply'] = True

    if fmt in ('glb', 'gltf'):
        _export_with_window_fallback(bpy.ops.export_scene.gltf, kwargs,
                                     path=path, selected_only=selected_only, apply_mods=apply_mods)
    elif fmt == 'fbx':
        _ops(bpy.ops.export_scene.fbx, **kwargs)
    elif fmt == 'obj':
        if bpy.app.background:
            # 后台模式 operator 的 poll 会失败，直接写文件
            _export_obj_direct(path, selected_only, apply_mods)
        else:
            try:
                _ops(bpy.ops.wm.obj_export, **kwargs)
            except AttributeError:
                _ops(bpy.ops.export_scene.obj, **kwargs)
    elif fmt == 'stl':
        try:
            _ops(bpy.ops.wm.stl_export, **kwargs)
        except AttributeError:
            _ops(bpy.ops.export_mesh.stl, **kwargs)
    elif fmt == 'usd':
        _ops(bpy.ops.wm.usd_export, **kwargs)
    else:
        raise ValueError('不支持的导出格式：%s（可用 glb/gltf/fbx/obj/stl/usd）' % fmt)

    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {'filepath': path, 'format': fmt, 'bytes': size}


def cmd_save_blend(params):
    path = params.get('filepath')
    if path:
        _ops(bpy.ops.wm.save_as_mainfile, filepath=path)
    else:
        if not bpy.data.filepath:
            raise ValueError('当前文件从未保存过，请给 filepath')
        _ops(bpy.ops.wm.save_mainfile)
    return {'filepath': bpy.data.filepath, 'saved': True}


# ══════════════════════════════════════════════════════ 几何导出 ══

def cmd_export_geometry(params):
    """把**活场景**的三角网格导出成 base64，供外部检查工具直接分析。

    这是解决「检查工具读磁盘、建模改活场景」割裂的关键：
    检查插件通过 MCP 调这个命令，拿到的就是模型眼前那个场景，
    不需要先 save_blend 再另开 blender -b 读文件。

    格式：每个 mesh 一条
      { id, part, positions: base64(Float32[3N]), indices: base64(Uint32[3T]) }
    坐标已转换到世界空间，索引已三角化。
    """
    scope = params.get('scope')
    max_objects = int(params.get('maxObjects') or 4000)
    max_tris = int(params.get('maxTriangles') or 2500000)
    with_modifiers = params.get('applyModifiers', True)

    # 解析 scope
    all_meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if scope:
        wanted = set(scope)
        targets = [o for o in all_meshes if o.name in wanted
                   or str(o.get('ptc_part') or '') in wanted]
    else:
        targets = all_meshes

    dg = bpy.context.evaluated_depsgraph_get()
    out = []
    skipped = []
    total_tris = 0

    for o in targets:
        if len(out) >= max_objects:
            skipped.append({'id': o.name, 'why': '超过 maxObjects'})
            continue

        me = None
        tmp_obj = None
        try:
            if with_modifiers and o.modifiers:
                tmp_obj = o.evaluated_get(dg)
                me = tmp_obj.to_mesh()
            else:
                me = o.to_mesh()

            if me is None or len(me.vertices) == 0:
                skipped.append({'id': o.name, 'why': '空网格'})
                continue

            me.calc_loop_triangles()
            nt = len(me.loop_triangles)
            if nt == 0:
                skipped.append({'id': o.name, 'why': '没有三角面'})
                continue
            if total_tris + nt > max_tris:
                skipped.append({'id': o.name, 'why': '超过 maxTriangles 预算'})
                continue

            nv = len(me.vertices)
            import numpy as np
            co = np.empty(nv * 3, dtype=np.float32)
            me.vertices.foreach_get('co', co)
            co = co.reshape(nv, 3).astype(np.float64)

            # 转到世界坐标
            mw = o.matrix_world
            M = np.array(mw, dtype=np.float64)
            world = (co @ M[:3, :3].T + M[:3, 3]).astype(np.float32)

            idx = np.empty(nt * 3, dtype=np.uint32)
            me.loop_triangles.foreach_get('vertices', idx)

            total_tris += nt
            part = str(o.get('ptc_part') or o.name)
            # Collection 归属：分组分析用它，比命名前缀可靠得多。
            # users_collection 是「这个对象直接挂在哪些 collection 里」。
            coll_names = [c.name for c in o.users_collection]
            out.append({
                'id': o.name,
                'part': part,
                'positions': base64.b64encode(world.tobytes()).decode('ascii'),
                'indices': base64.b64encode(idx.tobytes()).decode('ascii'),
                'vertices': int(nv),
                'triangles': int(nt),
                'hidden': bool(o.hide_viewport or o.hide_get()),
                'materials': [s.material.name for s in o.material_slots if s.material],
                'collections': coll_names,
                'parent': o.parent.name if o.parent else None,
            })
        except Exception as e:
            skipped.append({'id': o.name, 'why': str(e)[:120]})
        finally:
            try:
                if me is not None:
                    if tmp_obj is not None:
                        tmp_obj.to_mesh_clear()
                    else:
                        o.to_mesh_clear()
            except Exception:
                pass

    return {
        'meshes': out,
        'skipped': skipped,
        'triangles': total_tris,
        'file': bpy.data.filepath or None,
    }


# ══════════════════════════════════════════════════════ 其它 ══

def cmd_execute_code(params):
    code = params['code']
    capture = params.get('capture_output', True)

    # sys 必须在函数顶部导入：下面 except/finally 里都要用，
    # 只放在 if capture 分支里会让 capture_output=false 时报 NameError
    import sys
    import traceback as _tb

    out_lines = []
    old_stdout = None
    buf = None
    if capture:
        import io
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf

    try:
        ns = {'bpy': bpy, 'bmesh': bmesh, 'math': math, 'os': os}
        try:
            from mathutils import Vector, Matrix, Euler
            ns.update({'Vector': Vector, 'Matrix': Matrix, 'Euler': Euler})
        except Exception:
            pass
        exec(compile(code, '<mcp>', 'exec'), ns)
        result = ns.get('result')
    except Exception:
        # 报错要带上「哪一行、什么代码、完整 traceback」。
        # 只回 'faces' 这种信息量太低，模型根本不知道哪里错了。
        info = _tb.extract_tb(sys.exc_info()[2])
        line_no = None
        src_line = None
        for fr in info:
            if fr.filename == '<mcp>':
                line_no = fr.lineno
                src_line = (fr.line or '').strip()
        code_lines = code.splitlines()
        detail = []
        if line_no is not None:
            detail.append('第 %d 行：%s' % (line_no, src_line or ''))
            for k in range(max(0, line_no - 2), min(len(code_lines), line_no + 1)):
                mark = '>>' if k + 1 == line_no else '  '
                detail.append('%s %4d | %s' % (mark, k + 1, code_lines[k]))
        detail.append('')
        detail.append('完整 traceback：')
        detail.append(_tb.format_exc()[-1500:])
        raise RuntimeError('Python 执行失败\n' + '\n'.join(detail))
    finally:
        if capture and old_stdout is not None:
            sys.stdout = old_stdout
            out_lines = buf.getvalue().splitlines()

    return {
        'stdout': out_lines,
        'result': repr(result) if result is not None else None,
    }


def cmd_undo(params):
    steps = int(params.get('steps', 1))
    for _ in range(steps):
        try:
            _ops(bpy.ops.ed.undo)
        except Exception as e:
            return {'undone': False, 'error': str(e)}
    return {'undone': True, 'steps': steps}


def cmd_ping(params):
    return {
        'pong': True,
        'blender': bpy.app.version_string,
        'file': bpy.data.filepath or None,
        'objects': len(bpy.context.scene.objects),
    }


HANDLERS = {
    'get_scene_info': cmd_get_scene_info,
    'get_object_info': cmd_get_object_info,
    'get_blender_version': cmd_get_blender_version,
    'add_primitive': cmd_add_primitive,
    'add_text': cmd_add_text,
    'add_empty': cmd_add_empty,
    'extrude': cmd_extrude,
    'inset': cmd_inset,
    'bevel': cmd_bevel,
    'subdivide': cmd_subdivide,
    'loop_cut': cmd_loop_cut,
    'smooth_shade': cmd_smooth_shade,
    'transform_object': cmd_transform_object,
    'align_objects': cmd_align_objects,
    'mirror_object': cmd_mirror_object,
    'duplicate_object': cmd_duplicate_object,
    'add_modifier': cmd_add_modifier,
    'list_modifiers': cmd_list_modifiers,
    'remove_modifier': cmd_remove_modifier,
    'create_material': cmd_create_material,
    'assign_material': cmd_assign_material,
    'set_material_property': cmd_set_material_property,
    'list_materials': cmd_list_materials,
    # 物品分组（一个完整物品 = 一个 Collection）
    'group_create': cmd_group_create,
    'group_list': cmd_group_list,
    'group_split': cmd_group_split,
    'group_select': cmd_group_select,
    'group_delete': cmd_group_delete,
    # UV 映射（PBR 贴图精度的前提）
    'uv_unwrap': cmd_uv_unwrap,
    'uv_pack': cmd_uv_pack,
    'uv_scale': cmd_uv_scale,
    'uv_inspect': cmd_uv_inspect,
    'uv_auto_all': cmd_uv_auto_all,
    # PBR 贴图制作全套
    # 光影与渲染
    'setup_render': cmd_setup_render,
    'render_preview': cmd_render_preview,
    'select_objects': cmd_select_objects,
    'delete_objects': cmd_delete_objects,
    'rename_object': cmd_rename_object,
    'join_objects': cmd_join_objects,
    'separate_object': cmd_separate_object,
    'set_origin': cmd_set_origin,
    'render_view': cmd_render_view,
    'render_image': cmd_render_image,
    'set_camera': cmd_set_camera,
    'set_viewport_shading': cmd_set_viewport_shading,
    'import_file': cmd_import_file,
    'export_file': cmd_export_file,
    'save_blend': cmd_save_blend,
    'execute_code': cmd_execute_code,
    'undo': cmd_undo,
    'ping': cmd_ping,
    'export_geometry': cmd_export_geometry,
}

# 这些命令内部调用了 bpy.ops，必须在主线程执行。
# 其余命令只读写数据（bpy.data / 对象属性），可以在任意线程串行执行。
RENDER_COMMANDS = {'render_view', 'render_image', 'render_preview'}

MAIN_THREAD_COMMANDS = {
    'add_primitive', 'add_text', 'add_empty',
    'extrude', 'inset', 'bevel', 'subdivide', 'loop_cut', 'smooth_shade',
    'mirror_object', 'duplicate_object',
    'add_modifier', 'remove_modifier',
    'join_objects', 'separate_object', 'set_origin',
    'render_view', 'render_image',
    'import_file', 'export_file', 'save_blend',
    'undo',
    # PBR 烘焙与渲染都必须走主线程：
    #   bake 内部调用 bpy.ops.object.bake，需要 UI 上下文
    #   setup_render 改 render engine 会触发 depsgraph 更新
    #   render_preview 调 bpy.ops.render.render
    'setup_render', 'render_preview',
    # UV 操作全部要进编辑模式（bpy.ops.object.mode_set），必须走主线程
    'uv_unwrap', 'uv_pack', 'uv_scale', 'uv_auto_all',
    # 分组操作会改 collection 层级和对象链接
    'group_create', 'group_split', 'group_select', 'group_delete',
}


# ══════════════════════════════════════════════════════ 服务器 ══

class MCPCommandServer:
    """在后台线程里跑 socket 服务器。

    执行策略分两路（这是 Blender 的硬限制决定的）：

    1. **纯数据操作**（读属性、改 location/scale、创建材质、设置修改器参数）
       直接在 socket 线程里执行，用一把锁串行化。这类操作不碰 bpy.ops，
       在任何模式下都安全。

    2. **bpy.ops 操作**（primitive_add、bevel、join、render、import…）
       必须在主线程执行。这里用 bpy.app.timers 注册一个常驻泵，
       把命令投递到主线程队列里执行。

    为什么以前只有队列不行：`bpy.app.timers` 在 `blender -b` 下没有事件循环
    驱动，泵永远不跑。所以这里**主动**用 `bpy.app.timers` 注册泵的同时，
    对纯数据命令走直通路径——后台模式下至少查询类命令能工作，
    GUI 模式下全部命令都能工作。

    检测方法：判断当前是否处于后台模式（`bpy.app.background`），
    后台模式下如果命令需要 ops 又没有泵在跑，就明确报错而不是死等。
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.thread = None
        self.running = False
        self.bpy_lock = threading.Lock()
        # 需要主线程执行的任务：{id: {'done':bool,'result':...,'error':...}}
        self.main_tasks = {}
        self.main_lock = threading.Lock()
        self._task_seq = 0
        self._pump_registered = False

    def start(self):
        if self.running:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(8)
        self.running = True
        self._register_pump()
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    # ── 主线程泵 ──
    def _register_pump(self):
        """注册 bpy.app.timers 泵。GUI 模式下它会持续驱动主线程任务。"""
        if self._pump_registered:
            return

        def pump():
            if not self.running:
                return None
            pending = []
            with self.main_lock:
                for tid, task in list(self.main_tasks.items()):
                    if not task.get('claimed'):
                        task['claimed'] = True
                        pending.append((tid, task))
            for tid, task in pending:
                try:
                    result = task['fn']()
                    task['result'] = result
                except Exception as e:
                    task['error'] = e
                    task['trace'] = traceback.format_exc()[-1200:]
                task['done'] = True
            return 0.03

        try:
            bpy.app.timers.register(pump, first_interval=0.03, persistent=True)
            self._pump_registered = True
        except Exception:
            self._pump_registered = False

    def _run_on_main(self, fn, timeout=300):
        """把一个函数投递到主线程执行并等结果。

        GUI 模式：泵由 bpy.app.timers 驱动，命令进队列、主线程执行。
        后台模式（blender -b）：没有事件循环，泵不跑。

        后台模式的策略（分级）：
          - 命令在 MAIN_THREAD_COMMANDS 里 → 直接在本线程执行。这些命令
            已经改成 bmesh / bpy.data 实现，不需要界面上下文。
          - 例外：RENDER_COMMANDS（渲染）在非主线程会**直接崩进程**
            （EXCEPTION_ACCESS_VIOLATION，不可捕获），所以明确拒绝。
        """
        if bpy.app.background:
            if self._current_is_render:
                raise RuntimeError(
                    '渲染需要 Blender 的界面上下文，后台模式（-b）下执行会让进程崩溃。'
                    '请在带界面的 Blender 里运行 addon，再重试看图/渲染。'
                    '（建模、查询、材质等操作在后台模式可用。）')
            return fn()

        if not self._pump_registered:
            self._register_pump()
            if not self._pump_registered:
                raise RuntimeError(
                    '无法在主线程调度操作（bpy.app.timers 注册失败）。'
                    '这个操作需要主线程，当前环境不支持。')

        with self.main_lock:
            self._task_seq += 1
            tid = self._task_seq
            self.main_tasks[tid] = {'fn': fn, 'done': False, 'claimed': False}

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.main_lock:
                task = self.main_tasks.get(tid)
                if task and task.get('done'):
                    self.main_tasks.pop(tid, None)
                    if task.get('error'):
                        raise task['error']
                    return task.get('result')
            time.sleep(0.01)
        with self.main_lock:
            self.main_tasks.pop(tid, None)
        raise RuntimeError('主线程操作超时（%d 秒）' % timeout)

    def _execute(self, conn, msg):
        try:
            mtype = msg.get('type')
            params = msg.get('params') or {}
            handler = HANDLERS.get(mtype)
            if handler is None:
                resp = {'status': 'error', 'message': '未知命令：%s' % mtype}
            else:
                self._current_is_render = mtype in RENDER_COMMANDS
                try:
                    # 关键安全规则：**所有命令都走主线程**。
                    #
                    # 不这么做的后果是真实事故：execute_python 曾经留在 socket
                    # 线程里直接跑，模型执行的代码调用了
                    # collection.objects.link()，触发 depsgraph 更新，
                    # Blender 直接 EXCEPTION_ACCESS_VIOLATION 崩溃。
                    # 崩溃日志里的 PyThread_init_thread → RtlUserThreadStart
                    # 就是证据：那段代码跑在工作线程上。
                    #
                    # bpy 不是线程安全的。查询类操作常常侥幸不崩，但只要碰到
                    # 集合/对象增删、depsgraph 标签更新这类写操作，就会随机崩。
                    # 所以不再按命令分类豁免，统一回主线程——代价是每条命令多等
                    # 一个 timer tick（约 30ms），换来的是不崩。
                    #
                    # 后台模式（-b）没有事件循环，_run_on_main 内部会在当前线程
                    # 直接执行；后台模式只有一条线程在用 bpy，不存在竞争。
                    result = self._run_on_main(lambda: handler(params))
                    resp = {'status': 'success', 'result': result}
                except Exception as e:
                    resp = {'status': 'error',
                            'message': str(e),
                            'trace': traceback.format_exc()[-1500:]}
                finally:
                    self._current_is_render = False
        except Exception as e:
            resp = {'status': 'error', 'message': '内部错误：%s' % e}
        try:
            conn.sendall((json.dumps(resp, ensure_ascii=False, default=str) + '\n').encode('utf-8'))
        except Exception:
            pass

    def _accept_loop(self):
        while self.running:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        buffer = b''
        try:
            conn.settimeout(600)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                if b'\n' not in buffer:
                    continue
                line, buffer = buffer.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode('utf-8'))
                except Exception as e:
                    conn.sendall((json.dumps({'status': 'error',
                                              'message': 'JSON 解析失败：%s' % e}) + '\n').encode('utf-8'))
                    return
                self._execute(conn, msg)
                return  # 每条连接只处理一条命令
        except socket.timeout:
            pass
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


_server = None


# ══════════════════════════════════════════════════════ UI ══

class MCP_OT_start(bpy.types.Operator):
    bl_idname = 'mcp.start_server'
    bl_label = 'Start MCP Server'
    bl_description = '启动本地 MCP 命令服务器'

    def execute(self, context):
        global _server
        sc = context.scene
        try:
            _server = MCPCommandServer(HOST, sc.mcp_port)
            _server.start()
            self.report({'INFO'}, 'MCP 服务器已启动：%s:%d' % (HOST, sc.mcp_port))
        except OSError as e:
            self.report({'ERROR'}, '启动失败：%s（端口可能被占用）' % e)
        return {'FINISHED'}


class MCP_OT_stop(bpy.types.Operator):
    bl_idname = 'mcp.stop_server'
    bl_label = 'Stop MCP Server'
    bl_description = '停止本地 MCP 命令服务器'

    def execute(self, context):
        global _server
        if _server:
            _server.stop()
            _server = None
            self.report({'INFO'}, 'MCP 服务器已停止')
        return {'FINISHED'}


class MCP_PT_panel(bpy.types.Panel):
    bl_label = 'Blender MCP'
    bl_idname = 'MCP_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Blender MCP'

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        layout.prop(sc, 'mcp_port')
        running = _server is not None and _server.running
        layout.label(text='状态：' + ('运行中 ✓' if running else '未启动'), icon='CHECKMARK' if running else 'X')
        row = layout.row()
        row.enabled = not running
        row.operator('mcp.start_server', icon='PLAY')
        row = layout.row()
        row.enabled = running
        row.operator('mcp.stop_server', icon='PAUSE')
        layout.separator()
        layout.label(text='启动后由 MCP 服务器连接', icon='INFO')


CLASSES = (MCP_OT_start, MCP_OT_stop, MCP_PT_panel)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcp_port = bpy.props.IntProperty(
        name='端口', default=DEFAULT_PORT, min=1024, max=65535)


def unregister():
    global _server
    if _server:
        _server.stop()
        _server = None
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, 'mcp_port'):
        del bpy.types.Scene.mcp_port


if __name__ == '__main__':
    register()
