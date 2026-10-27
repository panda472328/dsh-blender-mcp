"""离线执行实际补光/恢复函数，不加载 bpy 或触碰活动场景。"""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace as NS

source = Path(__file__).resolve().parents[1] / 'addon' / 'blender_mcp.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
names = {'_ensure_viewable', '_restore_viewable'}
module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])


class Store:
    def __init__(self, factory):
        self.factory, self.items = factory, []

    def new(self, *args, **kwargs):
        item = self.factory(*args, **kwargs)
        self.items.append(item)
        return item

    def remove(self, item, **kwargs):
        self.items.remove(item)


class World:
    def __init__(self, name, nodes=True, strength=0.0, linked=False):
        self.name, self.use_nodes = name, nodes
        self.node_tree = NS(nodes={'Background': NS(inputs=[None, NS(default_value=strength, is_linked=linked)])})

    def copy(self):
        w = copy.deepcopy(self)
        worlds.items.append(w)
        return w


worlds = Store(World)
lights = Store(lambda name, type: NS(name=name, type=type))
objects = Store(lambda name, data: NS(name=name, data=data, type='LIGHT', hide_render=False))
bpy = NS(data=NS(worlds=worlds, lights=lights, objects=objects))
env = {'bpy': bpy}
exec(compile(module, str(source), 'exec'), env)

for world in [None, World('disabled', nodes=False), World('dark'), World('lit', strength=0.6),
              World('linked', linked=True)]:
    scene = NS(world=world, objects=[], collection=NS(objects=NS(link=lambda obj: None)))
    original = copy.deepcopy(world.__dict__) if world else None
    for _ in range(3):
        state = env['_ensure_viewable'](scene)
        assert len(objects.items) == 1
        if world and world.use_nodes and (world.name in ('lit', 'linked')):
            assert scene.world is world, 'valid or linked environment must be preserved'
        else:
            assert scene.world is not world
        try:
            raise RuntimeError('simulated renderer failure')
        except RuntimeError:
            pass
        finally:
            env['_restore_viewable'](scene, state)
        assert scene.world is world
        assert not objects.items and not lights.items and not worlds.items
        if world:
            assert world.__dict__ == original, 'original nodes and intensity remain untouched'

# 部分补光失败仍能清理已创建的数据块。
scene = NS(world=None, objects=[], collection=NS(objects=NS(link=lambda obj: (_ for _ in ()).throw(RuntimeError()))))
state = env['_ensure_viewable'](scene)
env['_restore_viewable'](scene, state)
assert scene.world is None and not lights.items and not objects.items
print('RENDER_CLEANUP_OK: missing, dark, disabled, linked environments and partial failures')
