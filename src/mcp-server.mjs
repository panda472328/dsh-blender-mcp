/**
 * 最小 MCP 服务器：Streamable HTTP 传输 + JSON-RPC 2.0。
 *
 * 为什么不引 SDK：宿主里没有 @modelcontextprotocol/server（它是 devDependency），
 * 而 MCP 的传输层其实就是「HTTP POST 收 JSON-RPC、回 JSON-RPC」，服务端要实现的
 * 只有 initialize / tools/list / tools/call / ping 这几个方法。手写不到 200 行，
 * 换来的好处是这个服务器能被任意 MCP 客户端直接用，不需要装依赖。
 *
 * 协议要点（2025-06-18 版）：
 *   - POST /mcp  body 是单个 JSON-RPC 请求或通知
 *   - 请求必须有 id，响应回到同一个 POST 的 body
 *   - 通知（无 id）回 202 空 body
 *   - 服务端可下发 Mcp-Session-Id 头，客户端后续请求带上
 *   - GET /mcp 是 SSE 流，本实现不提供（返回 405，客户端会退回 POST）
 */

import { createServer } from 'node:http'

export const PROTOCOL_VERSION = '2025-06-18'

/** JSON-RPC 错误码 */
export const RPC_ERROR = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
}

function send(res, status, body, extraHeaders = {}) {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(text),
    ...extraHeaders,
  })
  res.end(text)
}

function rpcOk(id, result) {
  return { jsonrpc: '2.0', id, result }
}

function rpcErr(id, code, message, data) {
  const error = { code, message }
  if (data !== undefined) error.data = data
  return { jsonrpc: '2.0', id, error }
}

/**
 * @param {object} opts
 * @param {string} opts.name 服务器名（会出现在 initialize 的 serverInfo）
 * @param {string} opts.version
 * @param {()=>Array} opts.listTools 返回工具定义数组
 * @param {(name:string,args:object,signal:AbortSignal)=>Promise<{content:Array,isError?:boolean}>} opts.callTool
 * @param {()=>string} [opts.instructions] 服务端说明，会进入模型系统提示
 * @param {number} [opts.port]
 * @param {string} [opts.host]
 */
export function createMcpServer(opts) {
  const {
    name, version, listTools, callTool,
    instructions, port = 8765, host = '127.0.0.1',
    log = () => {},
  } = opts

  /* 会话表：initialize 时发一个 id，后续请求带上。
   * 单用户本地服务，不做真正的会话隔离，只用来满足协议。 */
  const sessions = new Set()

  async function handleMessage(msg, signal) {
    const { id, method, params } = msg || {}
    const isNotification = id === undefined || id === null

    switch (method) {
      case 'initialize': {
        const sid = 'sess-' + Math.random().toString(36).slice(2, 12)
        sessions.add(sid)
        return {
          result: rpcOk(id, {
            protocolVersion: PROTOCOL_VERSION,
            capabilities: { tools: { listChanged: false } },
            serverInfo: { name, version },
            ...(instructions ? { instructions } : {}),
          }),
          sessionId: sid,
        }
      }

      case 'notifications/initialized':
      case 'notifications/cancelled':
        return { notification: true }

      case 'ping':
        return { result: rpcOk(id, {}) }

      case 'tools/list':
        return { result: rpcOk(id, { tools: listTools() }) }

      case 'tools/call': {
        const toolName = params && params.name
        const args = (params && params.arguments) || {}
        if (!toolName) {
          return { result: rpcErr(id, RPC_ERROR.INVALID_PARAMS, '缺少工具名') }
        }
        const known = listTools().some((t) => t.name === toolName)
        if (!known) {
          return { result: rpcErr(id, RPC_ERROR.INVALID_PARAMS, '没有这个工具：' + toolName) }
        }
        try {
          const out = await callTool(toolName, args, signal)
          return { result: rpcOk(id, out) }
        } catch (e) {
          /* 工具执行失败要作为「成功的 JSON-RPC 响应 + isError」返回，
           * 而不是 JSON-RPC 错误——这样模型能看到错误文本并自行纠正。 */
          return {
            result: rpcOk(id, {
              content: [{ type: 'text', text: '工具执行失败：' + String((e && e.message) || e) }],
              isError: true,
            }),
          }
        }
      }

      case 'resources/list':
        return { result: rpcOk(id, { resources: [] }) }

      case 'prompts/list':
        return { result: rpcOk(id, { prompts: [] }) }

      default:
        if (isNotification) return { notification: true }
        return { result: rpcErr(id, RPC_ERROR.METHOD_NOT_FOUND, '不支持的方法：' + method) }
    }
  }

  const server = createServer((req, res) => {
    /* 只接受 /mcp，其它路径给个可读的说明，方便浏览器直接打开确认服务活着 */
    if (req.url !== '/mcp') {
      send(res, 200, {
        name,
        version,
        protocol: PROTOCOL_VERSION,
        endpoint: '/mcp',
        tools: listTools().map((t) => t.name),
        hint: '这是 MCP 服务器。用 POST /mcp 发 JSON-RPC 请求。',
      })
      return
    }

    if (req.method === 'GET') {
      /* 不提供 SSE 推送流；客户端收到 405 会退回纯 POST 模式 */
      send(res, 405, { error: '本服务不支持 SSE 推送，请用 POST' }, { allow: 'POST, DELETE' })
      return
    }

    if (req.method === 'DELETE') {
      send(res, 200, { ok: true })
      return
    }

    if (req.method !== 'POST') {
      send(res, 405, { error: '只接受 POST' }, { allow: 'POST, DELETE' })
      return
    }

    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', async () => {
      let msg
      try {
        msg = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      } catch (e) {
        send(res, 200, rpcErr(null, RPC_ERROR.PARSE_ERROR, 'JSON 解析失败：' + String(e.message)))
        return
      }

      const signal = new AbortController().signal
      try {
        const out = await handleMessage(msg, signal)
        if (out.notification) {
          res.writeHead(202, { 'content-length': 0 })
          res.end()
          return
        }
        const headers = out.sessionId ? { 'mcp-session-id': out.sessionId } : {}
        send(res, 200, out.result, headers)
      } catch (e) {
        send(res, 200, rpcErr(msg && msg.id, RPC_ERROR.INTERNAL_ERROR, String((e && e.message) || e)))
      }
    })
    req.on('error', () => {
      try { send(res, 400, { error: '请求读取失败' }) } catch { /* ignore */ }
    })
  })

  return {
    server,
    listen() {
      return new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(port, host, () => {
          const addr = server.address()
          log('MCP 服务器已启动：http://' + host + ':' + addr.port + '/mcp')
          resolve({ host, port: addr.port, url: 'http://' + host + ':' + addr.port + '/mcp' })
        })
      })
    },
    close() {
      return new Promise((resolve) => server.close(() => resolve()))
    },
  }
}
