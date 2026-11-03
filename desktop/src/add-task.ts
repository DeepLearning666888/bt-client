/** 添加任务的客户端层：输入校验、请求构造、错误文案。
 *
 * 三件事都放在组件外面，理由很实际：
 *   - 校验和文案是纯函数，能直接单测边界（空串、少一位哈希、粘成网页地址）
 *   - 请求形状（URL / Bearer / JSON body）能用 stub fetch 断言，
 *     不必靠「点一下看看有没有任务」间接验证
 *   - 对话框只负责「显示错误 / 关闭自己」，不写任何合法性的 if/else
 *
 * 引擎的错误体是 `{"error":{"code":"...","message":"..."}}`，message 已经是中文，
 * 能透传就透传，只在状态码本身携带信息时（401/409）才换成更直白的说法。
 */

import { authHeaders, type EngineConfig } from './config'

export interface MagnetOk {
  ok: true
  /** 去掉首尾空白后的原文 */
  magnet: string
}

export interface MagnetBad {
  ok: false
  message: string
}

export type MagnetVerdict = MagnetOk | MagnetBad

/** btih 有两种写法：40 位 hex（v1 标准）与 32 位 base32（老客户端生成）。 */
const BTIH = /xt=urn:btih:([0-9a-f]{40}|[a-z2-7]{32})/i

export function normalizeMagnet(input: string): MagnetVerdict {
  const trimmed = input.trim()
  if (trimmed === '') return { ok: false, message: '请粘贴磁力链接' }

  const lower = trimmed.toLowerCase()
  if (lower.startsWith('http://') || lower.startsWith('https://')) {
    return { ok: false, message: '这是网页地址，暂时只支持 magnet: 开头的磁力链接' }
  }
  if (!lower.startsWith('magnet:?')) {
    return { ok: false, message: '磁力链接要以 magnet:? 开头' }
  }
  if (!BTIH.test(trimmed)) {
    return { ok: false, message: '链接里找不到完整的 btih，可能复制时被截断了' }
  }
  return { ok: true, magnet: trimmed }
}

/** 从引擎错误体里掏 message；结构不对就返回 null，绝不让 undefined 渗进文案。 */
function engineMessage(body: unknown): string | null {
  if (typeof body !== 'object' || body === null) return null
  const error = (body as Record<string, unknown>)['error']
  if (typeof error !== 'object' || error === null) return null
  const message = (error as Record<string, unknown>)['message']
  return typeof message === 'string' && message.trim() !== '' ? message.trim() : null
}

export function describeAddFailure(status: number, body: unknown): string {
  const detail = engineMessage(body)
  if (status === 401 || status === 403) return '引擎拒绝了请求（令牌无效），重启应用试试'
  if (status === 409) return '这个任务已经在列表里了'
  if (status === 400) return detail ?? '引擎认为这个磁力链接不合法'
  if (status === 404) return '引擎没有这个接口，版本可能不匹配'
  if (status >= 500) return detail === null ? '引擎处理失败' : `引擎处理失败：${detail}`
  return detail === null ? `添加失败（HTTP ${status}）` : `添加失败（HTTP ${status}）：${detail}`
}

export interface AddOutcome {
  infohash: string
}

/** 超时上限。引擎在本机回环，正常是毫秒级；15s 只用来兜「引擎没响应但端口没关」。 */
const ADD_TIMEOUT_MS = 15_000

export async function addTorrent(
  config: EngineConfig,
  magnet: string,
  timeoutMs = ADD_TIMEOUT_MS,
): Promise<AddOutcome> {
  const controller = new AbortController()
  const timer = setTimeout(() => {
    controller.abort()
  }, timeoutMs)

  let response: Response
  try {
    response = await fetch(`${config.baseUrl}/torrents`, {
      method: 'POST',
      headers: { ...authHeaders(config), 'Content-Type': 'application/json' },
      body: JSON.stringify({ magnet }),
      signal: controller.signal,
    })
  } catch (error) {
    // 引擎没起来、端口被防火墙拦、响应超时，三种都落到这里。
    // 不把原始 TypeError 抛给界面：它既没有可读性也没有指导性。
    const aborted = error instanceof Error && error.name === 'AbortError'
    throw new Error(aborted ? '等待引擎响应超时' : '连不上引擎，本地引擎可能没在运行')
  } finally {
    clearTimeout(timer)
  }

  // 错误响应体不一定是 JSON（502 之类可能是 HTML），解析失败不该盖掉更有用的文案
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(describeAddFailure(response.status, body))

  const infohash = typeof body === 'object' && body !== null
    ? (body as Record<string, unknown>)['infohash']
    : undefined
  if (typeof infohash !== 'string' || infohash === '') {
    throw new Error('引擎没有返回 infohash，任务状态未知')
  }
  return { infohash }
}
