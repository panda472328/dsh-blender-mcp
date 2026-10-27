# blender-mcp

让 DSH 直接看见并改动你正在做的 Blender 场景。

装上它，你的 Blender 就是场景建模模式的画布：场景里有什么、改完长什么样，它都能直接看到，并落到真实的几何与材质上。

**这是 [ptc-model-check](https://github.com/panda472328/dsh-ptc-model-check) 场景建模模式的配套包，两个包必须成对安装。** 只装 ptc-model-check 时，DSH 里没有可用的 Blender 通道，模型会转而自己开无头 Blender 跑脚本——你以为它在改你的场景，其实它看不见。完整安装步骤见 [安装文档](https://github.com/panda472328/dsh-ptc-model-check/blob/main/docs/install-blender-mcp.md)。

1.2.3 修复真实材质预览的临时补光清理：在临时世界副本上调整环境，完成或失败后恢复原世界、视口着色设置，并移除临时相机和补光数据。配合建模检查插件 2.28.0+ 使用真实材质渲染复核程序纹理。更新后需重新加载 Blender 插件；不要把仅显示材质色的预览当成节点渲染结果。

离线检查：`npm test` 验证工具通路，`python tests/verify_render_cleanup.py` 验证补光与世界恢复，不连接活动 Blender。

## 三个部件

```
addon/blender_mcp.py   Blender 侧：在 Blender 里开 socket 服务器，执行建模命令
src/server.mjs         MCP 服务器：实现 MCP 协议，把工具暴露给 DSH
cordis.patch.yml       DSH 侧：把 MCP 服务器接进来（mcp__blender__*）
```

## 安装

完整步骤（含 Blender 本体、四层依赖自检、七项验收）见配套插件仓库的 [docs/install-blender-mcp.md](https://github.com/panda472328/dsh-ptc-model-check/blob/main/docs/install-blender-mcp.md)。这里是最短路径：

**0. 把本包装进 DSH profile**（这一步才产生 `mcp__blender__*`）

从 <https://github.com/panda472328/dsh-blender-mcp/releases> 下载 `blender-mcp-1.2.3.zip` 解压，然后二选一：

- 让 AI 用 `plugin_manager` 安装解压目录；
- 手工在 `$env:DSH_PROFILE_DIR\package.json` 的 `dependencies` 加 `"@local/blender-mcp": "link:<解压目录绝对路径>"`，并在 `dsh.profile.bundles` 数组加 `"@local/blender-mcp"`，然后在 profile 目录执行 `pnpm install`。

**装完必须完全退出 DSH 再启动**（bundle 只在启动时读取，刷新页面无效）。

**1. 装 addon**

Blender → Edit → Preferences → Add-ons → Install… → 选 `addon/blender_mcp.py` → 勾选启用。

**2. 在 Blender 里启动服务器**

3D 视口按 `N` → 找到「Blender MCP」标签 → 点 **Start MCP Server**。
默认端口 9876，可在面板里改。

**3. 启动 MCP 服务器**

```bash
node src/server.mjs
```

默认监听 `http://127.0.0.1:8765/mcp`。

**4. 装进 DSH**

```
plugin_manager({ action: 'install_bundle', target: '<本目录绝对路径>' })
```

连上后工具会以 `mcp__blender__<名字>` 出现，例如 `mcp__blender__get_scene_info`。

## 命令行参数

```
node src/server.mjs --port 8765 --blender-port 9876
```

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--port` | `MCP_PORT` | 8765 | MCP 服务器端口 |
| `--host` | `MCP_HOST` | 127.0.0.1 | MCP 服务器地址 |
| `--blender-port` | `BLENDER_PORT` | 9876 | Blender addon 端口 |
| `--blender-host` | `BLENDER_HOST` | 127.0.0.1 | Blender addon 地址 |
| `--timeout` | `BLENDER_TIMEOUT` | 120000 | 单次命令超时毫秒 |

## 工具清单（39 个）

### 场景与查询

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `get_scene_info` | 场景信息 | 对象列表、活动对象、渲染设置、集合结构 |
| `get_object_info` | 对象详情 | 顶点/面数、包围盒、材质槽、修改器、自定义属性 |
| `get_blender_version` | Blender 版本 | 版本、可用渲染引擎、当前文件路径 |

### 创建几何体

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `add_primitive` | 添加基本体 | 立方体/球/圆柱/圆锥/圆环/平面/圆/网格/猴头/多面球 |
| `add_text` | 添加文字 | 3D 文字，可设挤出厚度、对齐 |
| `add_empty` | 添加空物体 | 控制点、阵列中心、约束目标 |

### 编辑几何体

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `extrude` | 挤出 | 沿法线或轴向挤出选中面 |
| `inset` | 内插面 | 面板凹槽、屏幕边框 |
| `bevel` | 倒角 | 边/顶点倒角，吃光的关键 |
| `subdivide` | 细分 | 增加面密度 |
| `loop_cut` | 环切 | 加支撑线、调分段 |
| `smooth_shade` | 平滑着色 | 平滑/平直切换 |

### 变换与对齐

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `transform_object` | 变换对象 | 位移/旋转/缩放，支持绝对或累加 |
| `align_objects` | 对齐对象 | 按轴对齐到左/中/右 |
| `mirror_object` | 镜像 | 沿轴镜像，可生成副本 |
| `duplicate_object` | 复制对象 | 批量复制并可指定偏移 |

### 修改器

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `add_modifier` | 添加修改器 | 细分曲面/镜像/阵列/倒角/布尔/实体化/螺旋/线框/精简/置换等 14 种 |
| `list_modifiers` | 查看修改器 | 列出某对象的修改器栈 |
| `remove_modifier` | 删除修改器 | 删除或应用（固化）修改器 |

### 材质与贴图

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `create_material` | 创建材质 | Principled BSDF，设颜色/金属度/粗糙度/透明/自发光 |
| `assign_material` | 指定材质 | 把材质贴到对象或某个槽 |
| `set_material_property` | 改材质属性 | 微调已有材质 |
| `list_materials` | 列出材质 | 所有材质及其使用情况 |

### 对象管理

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `select_objects` | 选择对象 | 按名称选择、设活动对象 |
| `delete_objects` | 删除对象 | ⚠ 危险操作 |
| `rename_object` | 重命名对象 | 改成部件名并写自定义属性 |
| `join_objects` | 合并对象 | 多个对象合成一个 |
| `separate_object` | 分离对象 | 按松散块或材质拆开 |
| `set_origin` | 设置原点 | 修「旋转绕错地方」的问题 |

### 观察与渲染

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `look` | 看画面 | 多视角出图（viewport/camera/前后左右顶底/等轴/多角度拼图） |
| `render_image` | 渲染出图 | 正式渲染，可指定引擎/分辨率/采样/保存路径 |
| `set_camera` | 设置相机 | 位置/朝向/焦距/正交 |
| `set_viewport_shading` | 设置视口着色 | solid/material/rendered/wireframe |

### 导入导出

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `import_file` | 导入文件 | glb/gltf/fbx/obj/stl/usd/blend |
| `export_file` | 导出文件 | glb/gltf/fbx/obj/stl/usd |
| `save_blend` | 保存工程 | 保存或另存 .blend |

### 其它

| 工具 | 中文名 | 做什么 |
|---|---|---|
| `execute_python` | 执行 Python | 主要执行通道，单件、批量、局部修复和材质节点均可自主编写 |
| `undo` | 撤销 | 撤销 N 步 |
| `ping` | 连通性检查 | 确认 Blender 连着 |

## 调用时机标签

1.2.2 默认使用 `script` 模式，仅向 AI 暴露 12 个脚本、场景读取、预览与文件工具。几何编辑、材质、UV 和分组由 AI 自主编写 `bpy` 脚本完成，减少重复工具描述。下面的完整工具目录保留作兼容参考；用 `--tool-profile all` 或 `MCP_TOOL_PROFILE=all` 可恢复全部工具，代码没有被删除。更新后需重启 MCP 服务并重新连接，现有进程不会自动切换。

每个工具的描述都以 `【何时调用】…【何时不调用】…【做什么】…` 开头。这是刻意设计的——MCP 工具的描述会进入模型上下文，写清时机能减少误用。

几条关键规则已经写进服务端的 `instructions`：

- 优先用 `execute_python` 自主编写建模和材质脚本；具体工具按需使用
- 脚本内直接命名、建立 Collection、修改器和节点，不强制另调专用工具
- 按逻辑批次查看实际视图，并配合建模检查插件核对数据
- 明确修改范围、支持局部重跑，保留用户原作和有价值的恢复点
- 导出后重开实际文件核对几何与材质

## 已知限制

| 限制 | 说明 |
|---|---|
| **后台模式（`blender -b`）不能渲染** | `look` / `render_image` 需要界面上下文，后台执行会让进程崩溃。addon 会明确拒绝而不是崩掉。**正常使用是 GUI 模式，不受影响。** |
| **后台模式 GLB 导出失败** | glTF 导出器需要 `context.window`。后台模式会给出可读提示，改用 OBJ 可正常导出。GUI 模式无此问题。 |
| 一次一个命令 | 每条 TCP 连接只处理一条命令，客户端每次新建连接 |
| 无认证 | addon 的 socket 没有加密认证，只监听 127.0.0.1 |

## 实现要点

**为什么自己写 MCP 协议**：宿主里没有 `@modelcontextprotocol/server`（它是 devDependency）。MCP 的 Streamable HTTP 传输就是「POST 收 JSON-RPC、回 JSON-RPC」，服务端只需实现 `initialize` / `tools/list` / `tools/call` / `ping`。手写约 200 行，换来零依赖、可被任意 MCP 客户端使用。

**为什么 addon 要区分主线程**：`bpy.ops` 必须在主线程执行。GUI 模式下用 `bpy.app.timers` 注册一个泵，命令进队列由主线程执行。后台模式没有事件循环，泵不跑，所以：

- 查询类和 bmesh 类操作直接在 socket 线程执行（已改成不依赖 `bpy.ops`）
- 渲染类明确拒绝（否则进程崩溃，且异常不可捕获）

**为什么编辑操作改用 bmesh**：`bpy.ops.mesh.*` 在无界面模式下 `poll()` 失败（需要 3D 视图区）。改用 `bmesh.ops.extrude_face_region` / `bevel` / `subdivide_edges` / `inset_region` / `bisect_plane` 后，GUI 和后台模式都能用。

## 自测

```bash
# addon 全量自测（28 项，不需要 MCP 服务器）
blender -b --python addon/_selftest.py

# MCP 协议层自测
node src/server.mjs --port 8799 &
node -e "..."
```
