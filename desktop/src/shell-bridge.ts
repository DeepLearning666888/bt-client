/** 与桌面壳的握手。
 *
 * 壳需要一条「前端真的拿到数据了」的证据。窗口存在、DOM 有节点、甚至 React 挂载完成，
 * 都不能证明 `/state` 请求成功过 —— 前端完全可能在「连不上引擎」的错误态里正常渲染。
 *
 * 所以前端在**首帧快照应用之后**回报一次 port + seq。两个字段合起来才构成端到端连通：
 * port 对上说明注入的端点是这一份，seq 非空说明数据真的到了。
 *
 * 浏览器形态下这是空操作：没有 `window.__TAURI__`，直接返回。
 * 握手失败不影响界面 —— 它是证据，不是功能。
 */

interface TauriGlobal {
  core?: {
    invoke?: unknown
  }
}

type InvokeFn = (command: string, args?: Record<string, unknown>) => Promise<unknown>

function findInvoke(): InvokeFn | null {
  const candidate = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__?.core?.invoke
  return typeof candidate === 'function' ? (candidate as InvokeFn) : null
}

let reported = false

export function notifyShellReady(port: number, seq: number): void {
  if (reported) return
  const invoke = findInvoke()
  if (invoke === null) return
  reported = true
  void invoke('frontend_ready', { port, seq }).catch(() => {
    // 壳不在（比如被单独打开的调试页面）时静默。不影响轮询。
  })
}

/** 仅供测试重置一次性标记。 */
export function resetShellBridge(): void {
  reported = false
}
