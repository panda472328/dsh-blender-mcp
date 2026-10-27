/**
 * 与 Blender addon 的通信层。
 *
 * 协议：本地 TCP socket，一行一个 JSON。
 *   请求  {"type":"<命令>", "params":{...}}
 *   响应  {"status":"success"|"error", "result":{...}} 或 {"status":"error","message":"..."}
 *
 * 为什么用 socket 而不是 HTTP：Blender 的 Python 进程里跑 HTTP 服务要额外依赖，
 * 而 socket 是标准库就有的；而且 addon 的服务器线程模型天然适合长连接。
 */

import { connect } from 'node:net'

export class BlenderError extends Error {
  constructor(message, code) {
    super(message)
    this.name = 'BlenderError'
    this.code = code || 'BLENDER_ERROR'
  }
}

export class BlenderClient {
  /**
   * @param {object} opts
   * @param {string} [opts.host]
   * @param {number} [opts.port]
   * @param {number} [opts.timeoutMs]
   */
  constructor(opts = {}) {
    this.host = opts.host || '127.0.0.1'
    this.port = opts.port || 9876
    this.timeoutMs = opts.timeoutMs || 120000
  }

  /**
   * 发一条命令，等一条响应。
   * @param {string} type
   * @param {object} params
   * @param {AbortSignal} [signal]
   */
  send(type, params = {}, signal) {
    return new Promise((resolve, reject) => {
      let settled = false
      const finish = (fn, arg) => {
        if (settled) return
        settled = true
        cleanup()
        fn(arg)
      }

      const socket = connect({ host: this.host, port: this.port })
      let buffer = ''

      const timer = setTimeout(() => {
        finish(reject, new BlenderError(
          'Blender 在 ' + Math.round(this.timeoutMs / 1000) + ' 秒内没有响应命令「' + type + '」。'
          + '可能原因：场景太大、操作卡住、或 addon 没在运行。',
          'TIMEOUT',
        ))
        socket.destroy()
      }, this.timeoutMs)

      const onAbort = () => {
        finish(reject, new BlenderError('操作被取消', 'ABORTED'))
        socket.destroy()
      }

      function cleanup() {
        clearTimeout(timer)
        if (signal) signal.removeEventListener('abort', onAbort)
        try { socket.destroy() } catch { /* ignore */ }
      }

      if (signal) {
        if (signal.aborted) { onAbort(); return }
        signal.addEventListener('abort', onAbort, { once: true })
      }

      socket.setEncoding('utf8')

      socket.on('connect', () => {
        try {
          socket.write(JSON.stringify({ type, params }) + '\n')
        } catch (e) {
          finish(reject, new BlenderError('发送失败：' + String(e.message), 'SEND_FAILED'))
        }
      })

      socket.on('data', (chunk) => {
        buffer += chunk
        const nl = buffer.indexOf('\n')
        if (nl < 0) return
        const line = buffer.slice(0, nl).trim()
        if (!line) return
        let msg
        try {
          msg = JSON.parse(line)
        } catch (e) {
          finish(reject, new BlenderError('Blender 返回了无法解析的内容：' + line.slice(0, 200), 'BAD_RESPONSE'))
          return
        }
        if (msg.status === 'success') {
          finish(resolve, msg.result ?? {})
        } else {
          finish(reject, new BlenderError(String(msg.message || 'Blender 报告错误'), 'BLENDER_REPORTED_ERROR'))
        }
      })

      socket.on('error', (e) => {
        const hint = e.code === 'ECONNREFUSED'
          ? '连不上 Blender（' + this.host + ':' + this.port + '）。请确认 Blender 已打开、'
            + 'addon 已启用、并在侧栏点了「Start MCP Server」。'
          : '连接 Blender 失败：' + String(e.message)
        finish(reject, new BlenderError(hint, e.code || 'CONNECT_FAILED'))
      })

      socket.on('close', () => {
        finish(reject, new BlenderError('Blender 关闭了连接（命令可能执行到一半）', 'CLOSED'))
      })
    })
  }

  /** 探测是否可连接，不抛错。 */
  async ping() {
    try {
      const r = await this.send('ping', {}, undefined)
      return { ok: true, info: r }
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) }
    }
  }
}
