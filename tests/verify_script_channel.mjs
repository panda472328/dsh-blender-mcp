/** 验证实际工具定义和转发，不连接 Blender 或启动服务器。 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildTools, selectTools, toMcpToolList } from '../src/tools.mjs'

const calls = []
const tools = buildTools({
  async send(type, params) { calls.push({ type, params }); return { output: 'created' } },
})
const python = tools.find((tool) => tool.name === 'execute_python')
assert.match(python.description, /主要建模和材质执行通道/)
assert.doesNotMatch(python.description, /兜底通道|一律用具体工具|必须用 rename_object/)
const code = 'import bpy\nmesh = bpy.data.meshes.new("壳体")'
const result = await python.handler({ code, capture_output: true })
assert.deepEqual(calls, [{ type: 'execute_code', params: { code, capture_output: true } }])
assert.match(result.content[0].text, /created/)
assert.ok(toMcpToolList(tools).some((tool) => tool.name === 'execute_python'))
const essentials = selectTools(tools)
assert.equal(essentials.length, 12)
for (const name of ['execute_python', 'ping', 'look', 'export_geometry', 'save_blend', 'export_file']) {
  assert.ok(essentials.some((tool) => tool.name === name), name + ' is available to modeling and checking')
}
for (const name of ['add_primitive', 'create_material', 'uv_unwrap', 'group_create']) {
  assert.ok(!essentials.some((tool) => tool.name === name), name + ' is hidden in script mode')
}
assert.equal(selectTools(tools, 'all').length, tools.length)
assert.throws(() => selectTools(tools, 'unknown'), /未知工具模式/)
const source = readFileSync(new URL('../src/server.mjs', import.meta.url), 'utf8')
assert.match(source, /execute_python 是主要执行通道/)
assert.match(source, /version,\s*\n\s*instructions,/)
console.log('SCRIPT_CHANNEL_OK: ' + tools.length + ' → ' + essentials.length + ' exposed tools')
