/**
 * Blender 建模工具声明。
 *
 * 组织原则：按建模流水线的实际顺序分组，而不是按 bpy API 分组。
 * 因为模型真正需要知道的是「现在这一步该调哪个」，不是「Blender 有哪些 API」。
 *
 * 每个工具都带：
 *   - name        模型可见的名字
 *   - title       中文名（列表用）
 *   - group       流水线分组
 *   - description 【何时调用】/【何时不调用】/【做什么】
 *   - inputSchema JSON Schema
 *   - handler     实际执行（转发给 addon）
 */

import { BlenderError } from './blender-client.mjs'

/* ─────────────────────────── Schema 小工具 ─────────────────────────── */

const str = (description, extra = {}) => ({ type: 'string', description, ...extra })
const num = (description, extra = {}) => ({ type: 'number', description, ...extra })
const int = (description, extra = {}) => ({ type: 'integer', description, ...extra })
const bool = (description, extra = {}) => ({ type: 'boolean', description, ...extra })
const arr = (description, items, extra = {}) => ({ type: 'array', description, items, ...extra })
const obj = (description, properties, required = []) => ({
  type: 'object', description, properties, ...(required.length ? { required } : {}),
})
const strArr = (description) => arr(description, { type: 'string' })
const vec3 = (description) => arr(description, { type: 'number' }, { minItems: 3, maxItems: 3 })
/* 枚举参数：模型只能在给定选项里选，避免传错值 */
const en = (description, values) => ({ type: 'string', description, enum: values })

/* ─────────────────────────── 工具定义 ─────────────────────────── */

export const TOOL_GROUPS = {
  scene: '场景与查询',
  create: '创建几何体',
  edit: '编辑几何体',
  transform: '变换与对齐',
  modify: '修改器',
  material: '材质与贴图',
  object: '对象管理',
  view: '观察与渲染',
  io: '导入导出',
  misc: '其它',
}

export function buildTools(client) {
  const call = (type, params, signal) => client.send(type, params, signal)

  /** 统一包装：把 handler 的返回值转成 MCP 的 content 结构。 */
  const wrap = (fn) => async (args, signal) => {
    const out = await fn(args || {}, signal)
    const text = typeof out === 'string' ? out : JSON.stringify(out, null, 2)
    return { content: [{ type: 'text', text }] }
  }

  const tools = [
    /* ══════════════════════ 场景与查询 ══════════════════════ */
    {
      name: 'get_scene_info',
      title: '场景信息',
      group: 'scene',
      description:
        '【何时调用】开始任何建模任务前先调一次，了解当前场景里有什么；'
        + '操作后想确认结果时也调它。'
        + '【何时不调用】已经知道场景状态、且刚查过没变化时不必重复。'
        + '【做什么】返回场景摘要：对象列表（名称/类型/位置/尺寸/材质）、活动对象、'
        + '渲染设置、世界环境、集合结构。默认只返回概要，对象很多时用 max_objects 限制。',
      inputSchema: obj('查询参数', {
        max_objects: int('最多返回多少个对象（默认 50）'),
        include_hidden: bool('是否包含隐藏对象（默认 false）'),
        detail: str('详细程度：brief（只要名字和类型）/ normal（默认）/ full（含顶点数、材质、修改器）', {
          enum: ['brief', 'normal', 'full'],
        }),
      }),
      handler: wrap((a, s) => call('get_scene_info', a, s)),
    },
    {
      name: 'get_object_info',
      title: '对象详情',
      group: 'scene',
      description:
        '【何时调用】需要看某个对象的精确数据时：顶点/面数、包围盒、材质槽、修改器栈、'
        + '变换矩阵、自定义属性。'
        + '【何时不调用】只需要知道「场景里有哪些东西」时用 get_scene_info 更省。'
        + '【做什么】返回一个对象的完整信息。',
      inputSchema: obj('查询参数', {
        name: str('对象名'),
      }, ['name']),
      handler: wrap((a, s) => call('get_object_info', a, s)),
    },
    {
      name: 'get_blender_version',
      title: 'Blender 版本',
      group: 'scene',
      description:
        '【何时调用】写脚本前想确认 API 差异时（4.x 和 5.x 有不少变化）。'
        + '【何时不调用】日常建模不需要。'
        + '【做什么】返回 Blender 版本、addon 版本、Python 版本、可用渲染引擎。',
      inputSchema: obj('无参数', {}),
      handler: wrap((a, s) => call('get_blender_version', a, s)),
    },

    /* ══════════════════════ 创建几何体 ══════════════════════ */
    {
      name: 'add_primitive',
      title: '添加基本体',
      group: 'create',
      description:
        '【何时调用】从零开始搭模型时，先用基本体起形（盒子、圆柱、球、平面等）。'
        + '【何时不调用】已经导入模型、或需要用曲线/雕刻等其它方式时。'
        + '【做什么】在指定位置创建基本体。支持 cube/sphere/cylinder/cone/torus/plane/circle/grid/monkey。'
        + '可以指定尺寸、位置、旋转、分段数。返回新对象名。',
      inputSchema: obj('创建参数', {
        type: str('基本体类型', {
          enum: ['cube', 'sphere', 'cylinder', 'cone', 'torus', 'plane', 'circle', 'grid', 'monkey', 'ico_sphere'],
        }),
        name: str('对象名（省略则自动命名）'),
        location: vec3('位置 [x,y,z]，默认 [0,0,0]'),
        rotation: vec3('旋转（弧度）[x,y,z]'),
        scale: vec3('缩放 [x,y,z]'),
        size: num('整体尺寸（不同类型含义略有差异，默认 1）'),
        segments: int('分段数（球/圆柱/圆环用）'),
        vertices: int('顶点数（圆柱/圆锥/圆用）'),
        radius: num('半径（球/圆柱/圆环用）'),
        depth: num('高度（圆柱/圆锥用）'),
        align: str('对齐方式：WORLD（默认）/ VIEW / CURSOR', { enum: ['WORLD', 'VIEW', 'CURSOR'] }),
      }, ['type']),
      handler: wrap((a, s) => call('add_primitive', a, s)),
    },
    {
      name: 'add_text',
      title: '添加文字',
      group: 'create',
      description:
        '【何时调用】需要 3D 文字时（logo、标识、刻字）。'
        + '【何时不调用】不需要文字时。'
        + '【做什么】创建 3D 文字对象，可设内容、大小、挤出厚度、对齐方式。',
      inputSchema: obj('文字参数', {
        text: str('文字内容'),
        name: str('对象名'),
        size: num('字号（默认 1）'),
        extrude: num('挤出厚度（默认 0）'),
        location: vec3('位置'),
        rotation: vec3('旋转（弧度）'),
        align_x: str('水平对齐：LEFT/CENTER/RIGHT', { enum: ['LEFT', 'CENTER', 'RIGHT'] }),
        align_y: str('垂直对齐：TOP/CENTER/BOTTOM', { enum: ['TOP', 'CENTER', 'BOTTOM'] }),
      }, ['text']),
      handler: wrap((a, s) => call('add_text', a, s)),
    },
    {
      name: 'add_empty',
      title: '添加空物体',
      group: 'create',
      description:
        '【何时调用】需要父子关系的控制点、阵列中心、约束目标时。'
        + '【何时不调用】不需要辅助对象时。'
        + '【做什么】创建空物体（empty），可选显示样式。',
      inputSchema: obj('空物体参数', {
        name: str('对象名'),
        location: vec3('位置'),
        display_type: str('显示样式：PLAIN/AXES/CIRCLE/SPHERE/CUBE/ARROWS/SINGLE_ARROW', {
          enum: ['PLAIN', 'AXES', 'CIRCLE', 'SPHERE', 'CUBE', 'ARROWS', 'SINGLE_ARROW'],
        }),
        size: num('显示尺寸'),
      }),
      handler: wrap((a, s) => call('add_empty', a, s)),
    },

    /* ══════════════════════ 编辑几何体 ══════════════════════ */
    {
      name: 'extrude',
      title: '挤出',
      group: 'edit',
      description:
        '【何时调用】从平面/面片长厚度、从轮廓长形体时——这是硬表面建模最常用的操作。'
        + '【何时不调用】用修改器能解决时优先用修改器（可逆、参数化）。'
        + '【做什么】对选中对象的面执行挤出。可指定挤出距离和方向轴。'
        + '需要先选中对象；可选先选中特定面。',
      inputSchema: obj('挤出参数', {
        name: str('对象名'),
        distance: num('挤出距离（默认 0.1）'),
        axis: str('挤出方向：NORMAL（默认，沿法线）/ X / Y / Z', { enum: ['NORMAL', 'X', 'Y', 'Z'] }),
        faces: arr('只挤出这些面索引（省略则全选）', { type: 'integer' }),
      }, ['name']),
      handler: wrap((a, s) => call('extrude', a, s)),
    },
    {
      name: 'inset',
      title: '内插面',
      group: 'edit',
      description:
        '【何时调用】做面板凹槽、屏幕边框、装饰线时。'
        + '【何时不调用】不需要在内侧留边时。'
        + '【做什么】对选中面做内插（inset），可控制厚度和深度。',
      inputSchema: obj('内插参数', {
        name: str('对象名'),
        thickness: num('内插厚度（默认 0.02）'),
        depth: num('内插深度（默认 0，正数凸出负数凹进）'),
      }, ['name']),
      handler: wrap((a, s) => call('inset', a, s)),
    },
    {
      name: 'bevel',
      title: '倒角',
      group: 'edit',
      description:
        '【何时调用】硬表面模型的边需要真实倒角来吃光（否则渲染出来边缘像纸片）；'
        + '产品建模的圆角也用它。'
        + '【何时不调用】需要锐利直角时不要倒角。'
        + '【做什么】对选中对象的边做倒角，可控制宽度和段数（段数越多越圆）。',
      inputSchema: obj('倒角参数', {
        name: str('对象名'),
        width: num('倒角宽度（默认 0.01）'),
        segments: int('段数（默认 2，越大越圆滑）'),
        affect: str('作用于：EDGES（默认）/ VERTICES', { enum: ['EDGES', 'VERTICES'] }),
      }, ['name']),
      handler: wrap((a, s) => call('bevel', a, s)),
    },
    {
      name: 'subdivide',
      title: '细分',
      group: 'edit',
      description:
        '【何时调用】需要更多面来做细节、或配合雕刻前需要足够密度时。'
        + '【何时不调用】只想让表面光滑时用细分曲面修改器（subsurf）更省面。'
        + '【做什么】对选中对象细分，可指定切割次数。',
      inputSchema: obj('细分参数', {
        name: str('对象名'),
        cuts: int('切割次数（默认 1）'),
      }, ['name']),
      handler: wrap((a, s) => call('subdivide', a, s)),
    },
    {
      name: 'loop_cut',
      title: '环切',
      group: 'edit',
      description:
        '【何时调用】需要在中段加支撑线（配合细分曲面做硬边）、或调整形体分段时。'
        + '【何时不调用】用 subdivide 能解决时。'
        + '【做什么】在对象上添加环形切线。可指定轴向和切几刀。',
      inputSchema: obj('环切参数', {
        name: str('对象名'),
        axis: str('沿哪个轴切：X / Y / Z', { enum: ['X', 'Y', 'Z'] }),
        count: int('切几刀（默认 1）'),
        offset: num('偏移量 0~1（默认 0.5 = 正中）'),
      }, ['name']),
      handler: wrap((a, s) => call('loop_cut', a, s)),
    },
    {
      name: 'smooth_shade',
      title: '平滑着色',
      group: 'edit',
      description:
        '【何时调用】曲面物体（球、圆柱、有机形体）需要看起来圆滑时。'
        + '【何时不调用】硬表面需要棱角分明的面时不要平滑。'
        + '【做什么】切换对象的面着色方式为平滑或平直。',
      inputSchema: obj('着色参数', {
        name: str('对象名'),
        smooth: bool('true = 平滑（默认），false = 平直'),
        auto_smooth_angle: num('自动平滑角度（度，可选；设置后按角度混合）'),
      }, ['name']),
      handler: wrap((a, s) => call('smooth_shade', a, s)),
    },

    /* ══════════════════════ 变换与对齐 ══════════════════════ */
    {
      name: 'transform_object',
      title: '变换对象',
      group: 'transform',
      description:
        '【何时调用】摆放对象位置、调整尺寸、旋转朝向时。'
        + '【何时不调用】只是改参数（如圆柱半径）时用对应创建/修改工具更准。'
        + '【做什么】设置对象的位移/旋转/缩放。可指定是绝对设置还是相对累加。',
      inputSchema: obj('变换参数', {
        name: str('对象名'),
        location: vec3('位置 [x,y,z]'),
        rotation: vec3('旋转（弧度）[x,y,z]'),
        scale: vec3('缩放 [x,y,z]'),
        mode: str('模式：SET（绝对值，默认）/ ADD（累加）/ MULTIPLY', { enum: ['SET', 'ADD', 'MULTIPLY'] }),
      }, ['name']),
      handler: wrap((a, s) => call('transform_object', a, s)),
    },
    {
      name: 'align_objects',
      title: '对齐对象',
      group: 'transform',
      description:
        '【何时调用】多个部件需要对齐到同一平面/轴线上时（做对称结构、排列零件）。'
        + '【何时不调用】只有一个对象时无意义。'
        + '【做什么】把一组对象按指定轴对齐（左/中/右、上/中/下、前/中/后）。',
      inputSchema: obj('对齐参数', {
        names: strArr('要对齐的对象名列表'),
        axis: str('对齐轴：X / Y / Z', { enum: ['X', 'Y', 'Z'] }),
        align: str('对齐方式：MIN / CENTER（默认）/ MAX', { enum: ['MIN', 'CENTER', 'MAX'] }),
        relative_to: str('参照物：SELECTION（选中集合，默认）/ ACTIVE / CURSOR', {
          enum: ['SELECTION', 'ACTIVE', 'CURSOR'],
        }),
      }, ['names', 'axis']),
      handler: wrap((a, s) => call('align_objects', a, s)),
    },
    {
      name: 'mirror_object',
      title: '镜像',
      group: 'transform',
      description:
        '【何时调用】做左右对称的结构（车辆、人物、机械件）时，先做一半再镜像。'
        + '【何时不调用】对象本身已经对称时。'
        + '【做什么】沿指定轴镜像对象。可原地镜像或生成副本。',
      inputSchema: obj('镜像参数', {
        name: str('对象名'),
        axis: str('镜像轴：X / Y / Z', { enum: ['X', 'Y', 'Z'] }),
        duplicate: bool('是否生成副本（默认 true）'),
        mirror_point: vec3('镜像中心点（默认原点）'),
      }, ['name', 'axis']),
      handler: wrap((a, s) => call('mirror_object', a, s)),
    },
    {
      name: 'duplicate_object',
      title: '复制对象',
      group: 'transform',
      description:
        '【何时调用】需要重复的零件（螺丝、齿、栏杆）时；配合 offset 可以直接排成一排。'
        + '【何时不调用】只需要一个实例时。'
        + '【做什么】复制对象，可指定复制数量和每次的偏移。',
      inputSchema: obj('复制参数', {
        name: str('对象名'),
        count: int('复制几个（默认 1）'),
        offset: vec3('每个副本的偏移 [x,y,z]'),
        linked: bool('是否使用关联副本（共享网格数据，默认 false）'),
      }, ['name']),
      handler: wrap((a, s) => call('duplicate_object', a, s)),
    },

    /* ══════════════════════ 修改器 ══════════════════════ */
    {
      name: 'add_modifier',
      title: '添加修改器',
      group: 'modify',
      description:
        '【何时调用】**优先用修改器而不是直接改网格**：修改器可逆、参数化、能随时调。'
        + '倒角/细分曲面/镜像/阵列/布尔/实体化都该先考虑修改器。'
        + '【何时不调用】需要最终确定几何、或修改器做不到的操作时。'
        + '【做什么】给对象添加修改器。支持 subsurf/mirror/array/bevel/boolean/solidify/screw/wireframe 等。',
      inputSchema: obj('修改器参数', {
        name: str('对象名'),
        type: str('修改器类型', {
          enum: ['SUBSURF', 'MIRROR', 'ARRAY', 'BEVEL', 'BOOLEAN', 'SOLIDIFY', 'SCREW', 'WIREFRAME',
            'DECIMATE', 'DISPLACE', 'SIMPLE_DEFORM', 'LATTICE', 'CURVE', 'WEIGHTED_NORMAL'],
        }),
        props: obj('修改器属性（按类型不同）。常见：levels（细分级别）、width（倒角宽度）、'
          + 'count（阵列数量）、thickness（实体化厚度）、operation（布尔运算：UNION/DIFFERENCE/INTERSECT）、'
          + 'object（布尔目标对象名）'),
        apply: bool('是否立即应用（默认 false，保持可调）'),
      }, ['name', 'type']),
      handler: wrap((a, s) => call('add_modifier', a, s)),
    },
    {
      name: 'list_modifiers',
      title: '查看修改器',
      group: 'modify',
      description:
        '【何时调用】接手别人的场景、或忘了某个对象上加了什么修改器时。'
        + '【何时不调用】刚自己加过、清楚有什么时。'
        + '【做什么】列出一个对象的所有修改器及其参数。',
      inputSchema: obj('查询参数', { name: str('对象名') }, ['name']),
      handler: wrap((a, s) => call('list_modifiers', a, s)),
    },
    {
      name: 'remove_modifier',
      title: '删除修改器',
      group: 'modify',
      description:
        '【何时调用】修改器效果不对、或需要固化几何前清理时。'
        + '【何时不调用】只是参数不对时，改参数比删了重加好。'
        + '【做什么】删除指定修改器，或应用（固化）它。',
      inputSchema: obj('删除参数', {
        name: str('对象名'),
        modifier: str('修改器名（用 list_modifiers 查）'),
        action: str('动作：REMOVE（默认）/ APPLY（固化到网格）', { enum: ['REMOVE', 'APPLY'] }),
      }, ['name', 'modifier']),
      handler: wrap((a, s) => call('remove_modifier', a, s)),
    },

    /* ══════════════════════ 材质与贴图 ══════════════════════ */
    {
      name: 'create_material',
      title: '创建材质',
      group: 'material',
      description:
        '【何时调用】给对象上色、做金属/塑料/玻璃等质感时。'
        + '【何时不调用】只是临时预览用视口着色即可。'
        + '【做什么】创建 Principled BSDF 材质，可设基础色、金属度、粗糙度、透明/发光。'
        + '返回材质名。',
      inputSchema: obj('材质参数', {
        name: str('材质名'),
        base_color: arr('基础色 RGBA 或 RGB（0~1 浮点）', { type: 'number' }),
        metallic: num('金属度 0~1（默认 0）'),
        roughness: num('粗糙度 0~1（默认 0.5）'),
        alpha: num('透明度 0~1（1 = 不透明）'),
        emission_color: arr('自发光颜色', { type: 'number' }),
        emission_strength: num('自发光强度'),
      }, ['name']),
      handler: wrap((a, s) => call('create_material', a, s)),
    },
    {
      name: 'assign_material',
      title: '指定材质',
      group: 'material',
      description:
        '【何时调用】材质建好了要贴到对象上时。'
        + '【何时不调用】材质还没建（先用 create_material）。'
        + '【做什么】把已有材质指定给对象（可指定给某个面槽）。',
      inputSchema: obj('指定参数', {
        object: str('对象名'),
        material: str('材质名'),
        slot: int('材质槽索引（默认 0）'),
      }, ['object', 'material']),
      handler: wrap((a, s) => call('assign_material', a, s)),
    },
    {
      name: 'set_material_property',
      title: '改材质属性',
      group: 'material',
      description:
        '【何时调用】材质建好后要微调（调颜色、调粗糙度）时。'
        + '【何时不调用】还没建材质时。'
        + '【做什么】修改已有材质的属性。',
      inputSchema: obj('属性参数', {
        material: str('材质名'),
        base_color: arr('基础色', { type: 'number' }),
        metallic: num('金属度'),
        roughness: num('粗糙度'),
        alpha: num('透明度'),
        emission_strength: num('自发光强度'),
      }, ['material']),
      handler: wrap((a, s) => call('set_material_property', a, s)),
    },
    {
      name: 'list_materials',
      title: '列出材质',
      group: 'material',
      description:
        '【何时调用】想知道场景里有哪些材质可复用时。'
        + '【何时不调用】清楚有哪些材质时。'
        + '【做什么】列出所有材质及其关键属性、被哪些对象使用。',
      inputSchema: obj('查询参数', {}),
      handler: wrap((a, s) => call('list_materials', a, s)),
    },

    {
      name: 'uv_inspect',
      title: '检查 UV 质量',
      group: 'uv',
      description:
        '【何时调用】**做贴图之前先查一次**：UV 有问题的话贴图做得再好也是错的。'
        + '【何时不调用】刚从 uv_auto_all 出来、且没改过几何时不必重复查。'
        + '\n【做什么】对每个 mesh 报告：有没有 UV、UV 利用率、是否超出 0~1 范围，并列出问题。'
        + '利用率低 = 浪费贴图分辨率；超出 0~1 = 会平铺或裁切；没 UV = 只能用 Object 坐标，'
        + '会导致所有材质密度不一致。',
      inputSchema: obj('检查参数', {
        object: str('只查这个对象（省略 = 整个场景）'),
      }),
      handler: wrap((a, s) => call('uv_inspect', a, s)),
    },
    {
      name: 'uv_unwrap',
      title: '展 UV',
      group: 'uv',
      description:
        '【何时调用】对象没有 UV，或 UV 明显不对时。**这是精确贴图映射的前提。**'
        + '【何时不调用】已有合格 UV 时不必重展（重展会打乱已有的贴图对应关系）。'
        + '\n【做什么】按指定方式展开 UV。方式怎么选：'
        + '\n- smart_project（默认）：自动分块，万金油，先试它'
        + '\n- angle_based：按角度分缝，硬表面机械件最合适'
        + '\n- conformal：保角展开，有机形状/曲面'
        + '\n- sphere / cylinder / cube：球/管/方块这类规则形状用投影更快更准',
      inputSchema: obj('展开参数', {
        object: str('对象名（省略 = 活动对象）'),
        method: en('展开方式', ['smart_project', 'angle_based', 'conformal',
          'sphere', 'cylinder', 'cube']),
        angle_limit: num('smart_project 的角度阈值（度，默认 66，越小分块越多）'),
        island_margin: num('UV 岛之间的间距（默认 0.02，太小会串色）'),
        area_weight: num('面积权重（默认 0，越大越倾向于保留大小比例）'),
        correct_aspect: bool('是否校正长宽比（默认 true）'),
        scale_to_bounds: bool('是否缩放到 0~1 范围（默认 true）'),
      }),
      handler: wrap((a, s) => call('uv_unwrap', a, s)),
    },
    {
      name: 'uv_pack',
      title: '重排 UV 岛',
      group: 'uv',
      description:
        '【何时调用】UV 利用率低（uv_inspect 报 <30%），或 unwrap 后 UV 岛散落时。'
        + '【何时不调用】利用率已经很高（>70%）时收益不大。'
        + '\n【做什么】把 UV 岛紧凑排布到 0~1 空间内，'
        + '**相当于免费提高贴图精度**——同样的贴图能覆盖更多细节。',
      inputSchema: obj('排布参数', {
        object: str('对象名（省略 = 活动对象）'),
        margin: num('岛间距（默认 0.003，越小塞得越紧但可能串色）'),
        rotate: bool('是否旋转岛以塞得更紧（默认 true）'),
        merge_overlap: bool('是否合并重叠的岛（默认 false，开之前确认真的可以合并）'),
      }),
      handler: wrap((a, s) => call('uv_pack', a, s)),
    },
    {
      name: 'uv_scale',
      title: '统一 UV 密度',
      group: 'uv',
      description:
        '【何时调用】**做写实材质时很重要**：让不同物体的贴图密度一致。'
        + '【何时不调用】场景里只有一个物体、或不在乎密度一致性时。'
        + '\n【做什么】按「texel density」（每世界单位用多少像素）重设 UV 比例。'
        + '\n【为什么重要】不做这一步，一张 1024 贴图铺在 10cm 螺丝和 2m 墙面上，'
        + '密度差 20 倍——看起来就是一个精细一个糊。'
        + '\n【怎么选密度】小零件（螺丝、按钮）512~1024；'
        + '中件（家具、道具）256~512；大件（墙、地面）64~256。',
      inputSchema: obj('密度参数', {
        object: str('对象名（省略 = 活动对象）'),
        texel_density: num('每世界单位的像素数（默认 512）'),
        texture_size: int('贴图边长像素（默认 1024）'),
      }),
      handler: wrap((a, s) => call('uv_scale', a, s)),
    },
    {
      name: 'uv_auto_all',
      title: '批量处理全场景 UV',
      group: 'uv',
      description:
        '【何时调用】进入贴图阶段前，一次性把整个场景的 UV 都准备好。'
        + '【何时不调用】只想处理某几个对象时用 uv_unwrap + uv_scale 逐个做。'
        + '\n【做什么】对每个 mesh 依次做 unwrap → 统一密度 → pack。'
        + '这是「渲染前必做」的一步——**没有 UV 就没有精确的贴图映射**，'
        + '程序化材质只能靠 Object 坐标，密度会乱。',
      inputSchema: obj('批量参数', {
        objects: strArr('只处理这些对象（省略 = 整个场景）'),
        method: en('展开方式', ['smart_project', 'angle_based', 'conformal',
          'sphere', 'cylinder', 'cube']),
        texture_size: int('贴图边长像素（默认 1024）'),
        texel_density: num('每世界单位的像素数（默认 512）'),
        margin: num('UV 岛间距（默认 0.02）'),
        only_missing: bool('只处理没有 UV 的对象（默认 false）'),
      }),
      handler: wrap((a, s) => call('uv_auto_all', a, s)),
    },

    /* ══════════════════════ 光影与渲染 ══════════════════════ */
    {
      name: 'setup_render',
      title: '配置渲染与光影',
      group: 'render',
      description:
        '【何时调用】材质做完要出效果图前；或渲染结果不对（过曝/发灰/太暗）时。'
        + '【何时不调用】只想快速看一眼形体时用 look 或 model_loop 的五视图（更省时间）。'
        + '【做什么】一次配好渲染引擎、采样数、色彩管理、世界光、三点布光。'
        + '色彩管理会自动选 AgX/Filmic（避免高光死白），世界光给环境亮度，'
        + '三点布光给 key/fill/rim 三盏面光。'
        + '【为什么需要】默认渲染设置出来的图通常发灰、过曝或死黑，'
        + '手调这些参数很费时且容易漏项。',
      inputSchema: obj('渲染参数', {
        engine: en('引擎：eevee=快 / cycles=真实但慢 / workbench=纯灰模', ['eevee', 'cycles', 'workbench']),
        samples: int('采样数（eevee 建议 64，cycles 建议 128）'),
        resolution: arr('分辨率 [宽, 高]', { type: 'integer' }),
        view_transform: str('色彩变换：AgX / Filmic / Standard'),
        world_color: arr('世界环境色 [r,g,b]', { type: 'number' }),
        world_strength: num('环境光强度（默认 1.0，室内可 0.3~0.6）'),
        add_lights: bool('是否加三点布光（默认 true）'),
      }),
      handler: wrap((a, s) => call('setup_render', a, s)),
    },
    {
      name: 'render_preview',
      title: '渲染效果图',
      group: 'render',
      description:
        '【何时调用】调整材质/光影后要看实际渲染效果时。'
        + '【何时不调用】只看形体时用 look（快得多）；验收五视图用 model_loop。'
        + '【做什么】按当前渲染设置出一张效果图。'
        + '\n【相机怎么定】**场景里有相机就直接用，不会动它**——'
        + '你摆好的机位每次渲染都被保住。'
        + '只有场景**没有**相机时才自动建一个能看全场景的。'
        + '想强制重新自动构图（比如要一张全景），传 auto_frame: true。'
        + '\n和 look 的区别：look 走视口快照（快、糙、适合看形体），'
        + 'render_preview 走渲染管线（慢、准、能看到材质和光影的真实效果）。',
      inputSchema: obj('渲染参数', {
        filepath: str('输出 PNG 路径'),
        camera: str('用哪个相机（省略则用场景当前的相机；场景没相机才自动建）'),
        auto_frame: bool('是否强制重新自动框选场景（默认 false）。'
          + '不传时：有相机就用相机，没相机才自动建。'
          + '传 true 会**覆盖相机位置**，只在确实想要自动构图时用'),
        width: int('宽度像素（默认用场景设置）'),
        height: int('高度像素'),
        samples: int('采样数（覆盖场景设置）'),
      }, ['filepath']),
      handler: wrap((a, s) => call('render_preview', a, s)),
    },

    /* ══════════════════════ 物品分组（一个完整物品 = 一个 Collection）══════════════════════ */
    {
      name: 'group_create',
      title: '建物品组',
      group: 'group',
      description:
        '【何时调用】**每建完一个完整物品，就把它的零件归入一个组**——这是建模流程的强制步骤。'
        + '【何时不调用】零件还在散着建、物品没成型时。'
        + '\n【做什么】把一个物品的全部零件对象链到一个 Blender Collection 里。'
        + '\n【为什么用 Collection 而不是命名约定**】名字前缀（`桌_桌面`）只是**推断**，'
        + '改成 `桌面`、`桌腿` 这种不带分隔符的命名就失效了。'
        + 'Collection 是 Blender 的原生分组机制：Outliner 里看得见层级、'
        + '渲染/隐藏/导出都能按组操作，是**真实的分组**不是猜测。'
        + '\n【规则】一个完整物品 = 一个 Collection，零件本身只需命名（不必带前缀）。'
        + '\n【分组之后能做】按组检查穿模、按组隔离渲染五视图、按组比较精度一致性。',
      inputSchema: obj('建组参数', {
        group: str('物品组名（比如「桌子」「自行车」）'),
        objects: strArr('这个物品的全部零件对象名'),
      }, ['group', 'objects']),
      handler: wrap((a, s) => call('group_create', a, s)),
    },
    {
      name: 'group_list',
      title: '列出物品组',
      group: 'group',
      description:
        '【何时调用】想知道场景里有哪些物品组、各包含什么、以及**哪些 mesh 还没分组**时。'
        + '【何时不调用】刚建完组、结构清楚时。'
        + '\n【做什么】列出每个组的名称、零件列表、包围盒尺寸，'
        + '**并单独列出没分组的 mesh**——那通常是漏分组了。',
      inputSchema: obj('查询参数', {
        include_empty: bool('是否也包括没有 mesh 的空组（默认 false）'),
      }),
      handler: wrap((a, s) => call('group_list', a, s)),
    },
    {
      name: 'group_split',
      title: '自动分组',
      group: 'group',
      description:
        '【何时调用】已经建好一堆零件、命名规范（`桌_桌面`），想一次把组都建出来时。'
        + '【何时不调用】只有一两个物品、手工 group_create 更清楚时。'
        + '\n【做什么】按命名前缀（或父级对象）自动推断分组，批量建 Collection。'
        + '\n【先 dry_run 看推断结果**】传 dry_run: true 先看它打算怎么分，'
        + '确认无误再真正执行——自动推断可能把 `Cube_001`、`Cube_002` 这种默认名也分进去。',
      inputSchema: obj('自动分组参数', {
        mode: en('推断方式：prefix=按命名前缀 / parent=按父级对象', ['prefix', 'parent']),
        dry_run: bool('只预览不执行（默认 false）'),
      }),
      handler: wrap((a, s) => call('group_split', a, s)),
    },
    {
      name: 'group_select',
      title: '选中物品组',
      group: 'group',
      description:
        '【何时调用】要对某个物品的全部零件批量操作前（加修改器、改材质、整体移动）。'
        + '【何时不调用】只操作单个零件时。'
        + '\n【做什么】选中一个组的全部零件对象。',
      inputSchema: obj('选择参数', {
        group: str('物品组名'),
      }, ['group']),
      handler: wrap((a, s) => call('group_select', a, s)),
    },
    {
      name: 'group_delete',
      title: '删除物品组',
      group: 'group',
      description:
        '【何时调用】分组分错了要重来时。'
        + '【何时不调用】只是想改组内成员时——直接用 group_create 重新指定即可。'
        + '\n【做什么】删除一个 Collection。默认 **keep_objects: true**：'
        + '只解除分组关系，零件保留并移回场景根。传 false 才会连零件一起删。',
      inputSchema: obj('删除参数', {
        group: str('物品组名'),
        keep_objects: bool('保留零件对象（默认 true）。传 false 会连零件一起删'),
      }, ['group']),
      handler: wrap((a, s) => call('group_delete', a, s)),
    },

    /* ══════════════════════ 对象管理 ══════════════════════ */
    {
      name: 'select_objects',
      title: '选择对象',
      group: 'object',
      description:
        '【何时调用】需要对多个对象批量操作前（挤出、对齐、加修改器都作用于选中对象）。'
        + '【何时不调用】操作已经指定了 name 参数时，多数工具会自动选中目标。'
        + '【做什么】按名称选择/取消选择对象，可设活动对象。',
      inputSchema: obj('选择参数', {
        names: strArr('要选择的对象名'),
        deselect_others: bool('是否先取消其它对象的选择（默认 true）'),
        active: str('设为活动对象的名字'),
      }, ['names']),
      handler: wrap((a, s) => call('select_objects', a, s)),
    },
    {
      name: 'delete_objects',
      title: '删除对象',
      group: 'object',
      description:
        '【何时调用】删掉多余的辅助对象、做错的部件时。'
        + '【何时不调用】**危险操作**，删之前先确认对象名对；不确定时先用 get_scene_info 查。'
        + '【做什么】按名称删除对象。',
      inputSchema: obj('删除参数', {
        names: strArr('要删除的对象名'),
        keep_mesh_data: bool('是否保留网格数据（默认 false）'),
      }, ['names']),
      handler: wrap((a, s) => call('delete_objects', a, s)),
    },
    {
      name: 'rename_object',
      title: '重命名对象',
      group: 'object',
      description:
        '【何时调用】对象还是 Cube.001 这类默认名时，立刻改成有意义的部件名。'
        + '【何时不调用】名字已经清楚时。'
        + '【做什么】重命名对象，可选写自定义属性便于后续按部件名识别。',
      inputSchema: obj('重命名参数', {
        name: str('当前对象名'),
        new_name: str('新名字'),
        set_custom_prop: bool('是否同时写一个 ptc_part 自定义属性（默认 true）'),
      }, ['name', 'new_name']),
      handler: wrap((a, s) => call('rename_object', a, s)),
    },
    {
      name: 'join_objects',
      title: '合并对象',
      group: 'object',
      description:
        '【何时调用】多个零件应该成为一个整体时（比如螺丝的各个部分）。'
        + '【何时不调用】零件需要单独编辑/单独赋材质时不要合并——合并后不好拆。'
        + '【做什么】把多个对象合并成一个（网格合并，保留各自材质槽）。',
      inputSchema: obj('合并参数', {
        names: strArr('要合并的对象名（第一个作为结果对象）'),
        result_name: str('合并后的对象名'),
      }, ['names']),
      handler: wrap((a, s) => call('join_objects', a, s)),
    },
    {
      name: 'separate_object',
      title: '分离对象',
      group: 'object',
      description:
        '【何时调用】一个对象里混了多个部件需要拆开时；或按材质拆成多个对象。'
        + '【何时不调用】只是想要独立编辑时，用编辑模式即可。'
        + '【做什么】把一个对象按松散块或按材质分离成多个对象。',
      inputSchema: obj('分离参数', {
        name: str('对象名'),
        mode: str('分离方式：LOOSE（松散块，默认）/ MATERIAL（按材质）', { enum: ['LOOSE', 'MATERIAL'] }),
      }, ['name']),
      handler: wrap((a, s) => call('separate_object', a, s)),
    },
    {
      name: 'set_origin',
      title: '设置原点',
      group: 'object',
      description:
        '【何时调用】旋转/缩放时物体绕错地方转——通常就是原点位置不对。'
        + '【何时不调用】原点位置已经合适时。'
        + '【做什么】把对象原点移到几何中心/游标/指定点。',
      inputSchema: obj('原点参数', {
        name: str('对象名'),
        mode: str('模式：GEOMETRY（几何中心，默认）/ CURSOR / CENTER_OF_MASS / BOUNDS', {
          enum: ['GEOMETRY', 'CURSOR', 'CENTER_OF_MASS', 'BOUNDS'],
        }),
      }, ['name']),
      handler: wrap((a, s) => call('set_origin', a, s)),
    },

    /* ══════════════════════ 观察与渲染 ══════════════════════ */
    {
      name: 'look',
      title: '看画面',
      group: 'view',
      description:
        '【何时调用】**每改完一轮就看一次**——这是验收条件。看形状对不对、比例对不对、有没有穿模。'
        + '【何时不调用】纯数据查询（改名、查顶点数）不需要看。'
        + '【做什么】从指定视角出图并返回。'
        + '\n**view=angles 一次给你三个角度**（正视/侧视/等轴）纵向拼成一张——'
        + '**想「看全面」就用它**，不用连调五次。'
        + '\n**三种 shading 用途不同**：'
        + '\n  · `solid`（默认）—— 看**形体**，最快（angles 约 0.5 秒）。灰白，不看材质。'
        + '\n  · `material` —— 看**材质色**，仍是快速预览。'
        + '\n  · `rendered` —— 走真实引擎，**吃你摆的灯和环境光**（angles 约 1.2 秒）。'
        + '想看「黄昏暖阳穿过树林」这类光照效果，只有它能给你。'
        + '\n【注意】`rendered` 在**没有灯**的场景会全黑；这时它会临时补一盏太阳光让你能看清。',
      inputSchema: obj('看图参数', {
        view: str('视角：viewport（默认）/ camera / front / back / left / right / top / bottom / iso / angles', {
          enum: ['viewport', 'camera', 'front', 'back', 'left', 'right', 'top', 'bottom', 'iso', 'angles'],
        }),
        shading: str('着色：solid（默认，看形体，快）/ material（看材质色）/ rendered（真引擎，吃场景灯）/ wireframe / xray', {
          enum: ['solid', 'material', 'rendered', 'wireframe', 'xray'],
        }),
        width: int('图片宽度像素（默认 800）'),
        height: int('图片高度像素（默认 600）'),
        focus: strArr('只框选这些对象（省略 = 全部）'),
        hide_others: bool('是否隐藏其它对象（默认 false）'),
        engine: str('rendered 模式下用哪个引擎：BLENDER_EEVEE（默认，约 0.5 秒）/ CYCLES（慢，约 2 秒）'),
      }),
      handler: async (args, signal) => {
        const out = await call('render_view', args, signal)
        /* addon 返回 base64 PNG；转成 MCP 的 image content */
        if (out && out.image_base64) {
          return {
            content: [
              { type: 'text', text: out.summary || '已渲染' },
              { type: 'image', data: out.image_base64, mimeType: 'image/png' },
            ],
          }
        }
        return { content: [{ type: 'text', text: JSON.stringify(out, null, 2) }] }
      },
    },
    {
      name: 'render_image',
      title: '渲染出图',
      group: 'view',
      description:
        '【何时调用】要出正式效果图（带材质、灯光、全局光照）时；或要保存成品图到磁盘时。'
        + '【何时不调用】只是检查形状时用 look 的 solid 模式快得多（秒级 vs 分钟级）。'
        + '【做什么】用场景相机按当前渲染设置出图。可指定引擎、分辨率、采样数、保存路径。',
      inputSchema: obj('渲染参数', {
        engine: str('渲染引擎：BLENDER_EEVEE / BLENDER_WORKBENCH / CYCLES（默认沿用场景设置）'),
        resolution_x: int('宽（默认 1920）'),
        resolution_y: int('高（默认 1080）'),
        samples: int('采样数（默认 32）'),
        filepath: str('保存路径（省略则只返回图片不存盘）'),
        camera: str('用哪个相机（省略 = 场景活动相机）'),
      }),
      handler: async (args, signal) => {
        const out = await call('render_image', args, signal)
        const content = [{ type: 'text', text: out.summary || '已渲染' }]
        if (out && out.image_base64) {
          content.push({ type: 'image', data: out.image_base64, mimeType: 'image/png' })
        }
        return { content }
      },
    },
    {
      name: 'set_camera',
      title: '设置相机',
      group: 'view',
      description:
        '【何时调用】要固定构图、出图前调整取景时。'
        + '【何时不调用】只是自己看一眼时，look 的 angles 模式会自动框选。'
        + '【做什么】创建或调整相机：位置、朝向、焦距、正交/透视。',
      inputSchema: obj('相机参数', {
        name: str('相机对象名（省略 = 用活动相机，没有则新建）'),
        location: vec3('相机位置'),
        look_at: vec3('看向的点'),
        lens: num('焦距 mm（默认 50）'),
        ortho: bool('是否正交投影'),
        ortho_scale: num('正交尺度'),
      }),
      handler: wrap((a, s) => call('set_camera', a, s)),
    },
    {
      name: 'set_viewport_shading',
      title: '设置视口着色',
      group: 'view',
      description:
        '【何时调用】看线框检查拓扑、或想看材质效果时。'
        + '【何时不调用】默认着色够用时。'
        + '【做什么】切换视口着色模式与背景。',
      inputSchema: obj('着色参数', {
        shading: str('模式：solid / material / rendered / wireframe', {
          enum: ['solid', 'material', 'rendered', 'wireframe'],
        }),
        show_overlays: bool('是否显示叠加层（网格线等）'),
        background: str('背景：THEME / WORLD / VIEWPORT', { enum: ['THEME', 'WORLD', 'VIEWPORT'] }),
      }, ['shading']),
      handler: wrap((a, s) => call('set_viewport_shading', a, s)),
    },

    /* ══════════════════════ 导入导出 ══════════════════════ */
    {
      name: 'import_file',
      title: '导入文件',
      group: 'io',
      description:
        '【何时调用】要用现成模型/素材时。支持 glb/gltf/fbx/obj/stl/usd/blend。'
        + '【何时不调用】从零建模时。'
        + '【做什么】把外部模型文件导入当前场景，返回导入的对象名列表。',
      inputSchema: obj('导入参数', {
        filepath: str('文件绝对路径'),
        format: str('格式（省略则按扩展名推断）：glb / gltf / fbx / obj / stl / usd / blend', {
          enum: ['glb', 'gltf', 'fbx', 'obj', 'stl', 'usd', 'blend'],
        }),
        scale: num('整体缩放（默认 1）'),
      }, ['filepath']),
      handler: wrap((a, s) => call('import_file', a, s)),
    },
    {
      name: 'export_file',
      title: '导出文件',
      group: 'io',
      description:
        '【何时调用】模型做完了要交付/给引擎用时。'
        + '【何时不调用】还在迭代中，导出会有大量中间版本。'
        + '【做什么】把选中对象（或全部）导出为 glb/gltf/fbx/obj/stl/usd。',
      inputSchema: obj('导出参数', {
        filepath: str('导出路径'),
        format: str('格式：glb（默认）/ gltf / fbx / obj / stl / usd', {
          enum: ['glb', 'gltf', 'fbx', 'obj', 'stl', 'usd'],
        }),
        selected_only: bool('只导出选中对象（默认 true）'),
        apply_modifiers: bool('导出时应用修改器（默认 true）'),
      }, ['filepath']),
      handler: wrap((a, s) => call('export_file', a, s)),
    },
    {
      name: 'save_blend',
      title: '保存工程',
      group: 'io',
      description:
        '【何时调用】完成一个阶段性成果、或做危险操作前先存盘。'
        + '【何时不调用】每次小改动都存会积累大量无用版本。'
        + '【做什么】保存当前 .blend 文件。可另存为。',
      inputSchema: obj('保存参数', {
        filepath: str('另存为路径（省略 = 覆盖保存当前文件）'),
      }),
      handler: wrap((a, s) => call('save_blend', a, s)),
    },

    /* ══════════════════════ 其它 ══════════════════════ */
    {
      name: 'execute_python',
      title: '执行 Python',
      group: 'misc',
      description:
        '【何时调用】主要建模和材质执行通道，单件、批量、参数化、局部修复和节点材质均可自主编写脚本。'
        + '可直接使用 bpy、bmesh、mathutils，编写适合当前模型的局部函数，不必先寻找专用工具。'
        + '【何时不调用】已有场景读取或预览工具足够时可直接读取，避免不必要的脚本执行。'
        + '【做什么】在**正在运行的 Blender** 里执行 Python，可以直接用 bpy、bmesh、mathutils。'
        + 'print 的内容会出现在返回里；最后表达式的值也会返回。'
        + '【执行要求】① 有价值的阶段成果和破坏性操作前保存恢复点，不要求每条只读脚本前存盘；'
        + '② 用明确命名、对象引用或局部更新保证可重复执行，只清理自己生成且确认可替换的对象，保留用户原作；'
        + '③ 报错会原样返回，照着改，不要盲试；'
        + '④ 执行是同步阻塞的，长脚本会卡住界面，这是正常的；'
        + '⑤ 可在脚本内直接设置对象名、Collection、修改器和材质节点，不强制补调用命名或分组工具；'
        + '⑥ 完成一个逻辑批次后检查几何与实际视图，纯技术通过不能证明造型与细节到位。',
      inputSchema: obj('执行参数', {
        code: str('要执行的 Python 代码。可以用 bpy、bmesh、mathutils 等。'
          + 'print 内容会出现在返回里；最后表达式的值也会返回。'
          + '建议：明确修改范围与对象名，支持局部重跑，保留原场景和恢复点。'),
        capture_output: bool('是否捕获 print 输出（默认 true）'),
      }, ['code']),
      handler: wrap((a, s) => call('execute_code', a, s)),
    },
    {
      name: 'undo',
      title: '撤销',
      group: 'misc',
      description:
        '【何时调用】刚做错一步，想退回去时。'
        + '【何时不调用】不确定撤销几步才到想要的状态时——宁可重新做，撤销过头更难修。'
        + '【做什么】撤销 N 步（Blender 的撤销栈）。',
      inputSchema: obj('撤销参数', {
        steps: int('撤销几步（默认 1）'),
      }),
      handler: wrap((a, s) => call('undo', a, s)),
    },
    {
      name: 'ping',
      title: '连通性检查',
      group: 'misc',
      description:
        '【何时调用】怀疑 Blender 没连上时先调它，比直接操作再报错省事。'
        + '【何时不调用】刚成功调用过其它工具时不需要。'
        + '【做什么】检查与 Blender addon 的连接是否正常。',
      inputSchema: obj('无参数', {}),
      handler: async () => {
        const r = await client.ping()
        return {
          content: [{
            type: 'text',
            text: r.ok
              ? 'Blender 连接正常：' + JSON.stringify(r.info)
              : 'Blender 连不上：' + r.error,
          }],
          isError: !r.ok,
        }
      },
    },
    {
      name: 'export_geometry',
      title: '导出几何数据',
      group: 'scene',
      description:
        '【何时调用】外部检查工具需要分析**活场景**的网格时。'
        + '它的意义：检查工具通常读磁盘上的 .blend，而你正在改的是内存里的活场景，'
        + '两边会不一致。通过这个工具可以把活场景的几何直接取出来，避免"必须先存盘"。'
        + '【何时不调用】只需要看在不在、有几个对象时用 get_scene_info 就够（它更轻）。'
        + '【做什么】把场景里每个 mesh 的世界坐标三角网格导出成 base64：'
        + 'positions（Float32 三维坐标）+ indices（Uint32 三角索引）。'
        + '可选应用修改器、限制对象数与三角面预算。',
      inputSchema: obj('导出参数', {
        scope: strArr('只导出这些对象名或部件名（省略 = 全部 mesh）'),
        maxObjects: int('最多导多少个对象（默认 4000）'),
        maxTriangles: int('三角面总预算（默认 250 万）'),
        applyModifiers: bool('是否应用修改器后再导出（默认 true）'),
      }),
      handler: wrap((a, s) => call('export_geometry', a, s)),
    },
  ]

  return tools
}

/** 默认收紧模型可见工具面，保留 all 模式兼容现有调用。 */
export function selectTools(tools, profile = 'script') {
  if (profile === 'all') return [...tools]
  if (profile !== 'script') throw new Error('未知工具模式：' + profile + '（可选 script / all）')
  const essential = new Set([
    'execute_python', 'ping', 'get_scene_info', 'get_object_info', 'get_blender_version',
    'look', 'render_preview', 'render_image', 'import_file', 'export_file', 'save_blend', 'export_geometry',
  ])
  return tools.filter((tool) => essential.has(tool.name))
}

/** 转成 MCP tools/list 需要的结构（去掉内部字段）。 */
export function toMcpToolList(tools) {
  return tools.map((t) => ({
    name: t.name,
    title: t.title,
    description: t.description,
    inputSchema: t.inputSchema,
    annotations: {
      title: t.title,
      /* 只读工具标记：MCP 客户端可以据此决定要不要确认 */
      readOnlyHint: ['get_scene_info', 'get_object_info', 'get_blender_version', 'list_modifiers',
        'list_materials', 'look', 'ping'].includes(t.name),
      /* execute_python 不标 destructive：它是主要建模通道，
       * 标成"危险"会让客户端每次弹确认，实际上它和别的建模工具一样
       * 只是改场景。真正的破坏性操作是删除和撤销。 */
      destructiveHint: ['delete_objects', 'undo'].includes(t.name),
    },
  }))
}
