/** 引擎地址与令牌来源。
 *
 * M2a（当前阶段）从构建期环境变量读：浏览器里没有文件系统，读不到引擎写的
 * engine.endpoint.json。开发流程是先用 desktop/launcher.py 拉起引擎并打印这两个
 * 变量，再启动 vite。
 *
 * M2b（Tauri 壳）改为读 endpoint 文件 + sidecar 拉起引擎，见 specs/desktop.md。
 * 这里刻意把「配置从哪来」收在一个函数里，替换时只动这一处。
 */

export interface EngineConfig {
  baseUrl: string
  token: string
}

export function readEngineConfig(): EngineConfig | null {
  const env = import.meta.env as unknown as Record<string, string | undefined>
  const baseUrl = env['VITE_ENGINE_URL']?.trim()
  const token = env['VITE_ENGINE_TOKEN']?.trim()
  if (!baseUrl || !token) return null
  return { baseUrl: baseUrl.replace(/\/+$/, ''), token }
}

export function stateUrl(config: EngineConfig, since: number): string {
  return `${config.baseUrl}/state?since=${since}`
}

export function authHeaders(config: EngineConfig): Record<string, string> {
  return { Authorization: `Bearer ${config.token}` }
}
