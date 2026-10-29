/** 引擎地址与令牌来源。
 *
 * 两种形态，优先级从高到低：
 *
 * 1. **壳注入**（桌面形态）—— Tauri 在页面脚本执行之前写入 `window.__ENGINE__`。
 *    壳已经负责发现/拉起引擎，前端只管用。
 * 2. **构建期环境变量**（浏览器形态）—— M2a 的开发流程：先跑 launcher.py 拿到
 *    两个变量，再启动 vite。浏览器没有文件系统，读不到 engine.endpoint.json。
 *
 * 这里刻意把「配置从哪来」收在一个函数里：加第三种来源时只动这一处。
 */

export interface EngineConfig {
  baseUrl: string
  token: string
}

interface InjectedEngine {
  baseUrl?: unknown
  token?: unknown
}

declare global {
  interface Window {
    __ENGINE__?: InjectedEngine
  }
}

function normalize(baseUrl: string, token: string): EngineConfig | null {
  const trimmedUrl = baseUrl.trim()
  const trimmedToken = token.trim()
  if (trimmedUrl === '' || trimmedToken === '') return null
  return { baseUrl: trimmedUrl.replace(/\/+$/, ''), token: trimmedToken }
}

export function readEngineConfig(): EngineConfig | null {
  // 1) 壳注入。字段类型不对时当作没有注入，继续往下回退 ——
  //    壳坏掉不该连带把浏览器开发流程也弄挂。
  if (typeof window !== 'undefined') {
    const injected = window.__ENGINE__
    if (injected !== undefined && injected !== null) {
      if (typeof injected.baseUrl === 'string' && typeof injected.token === 'string') {
        const fromShell = normalize(injected.baseUrl, injected.token)
        if (fromShell !== null) return fromShell
      }
    }
  }

  // 2) 构建期环境变量
  const env = import.meta.env as unknown as Record<string, string | undefined>
  const baseUrl = env['VITE_ENGINE_URL']
  const token = env['VITE_ENGINE_TOKEN']
  if (typeof baseUrl !== 'string' || typeof token !== 'string') return null
  return normalize(baseUrl, token)
}

/** 从 baseUrl 取出端口，供壳的就绪握手回报。解析失败返回 0。 */
export function portOf(config: EngineConfig): number {
  try {
    const parsed = new URL(config.baseUrl)
    const port = Number(parsed.port)
    return Number.isInteger(port) && port > 0 ? port : 0
  } catch {
    return 0
  }
}

export function stateUrl(config: EngineConfig, since: number): string {
  return `${config.baseUrl}/state?since=${since}`
}

export function authHeaders(config: EngineConfig): Record<string, string> {
  return { Authorization: `Bearer ${config.token}` }
}
