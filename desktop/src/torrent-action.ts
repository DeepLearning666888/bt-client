/** 任务操作层：暂停、恢复、删除。
 *
 * 与添加分开放，因为它们的「成败如何判定」不一样：
 *   - 添加失败要区分「链接不对」「已存在」「引擎没起来」
 *   - 删除失败最常见的是「这个任务已经不在了」—— 那不是错误，是别人先动了手
 *
 * 三条约束写在这里，组件只负责显示：
 *   1. infohash 拼进 URL 之前必须是 40 位 hex（防路径穿越，见 specs/desktop.md）
 *   2. 删除必须显式传 delete_files，不靠引擎默认值
 *   3. 所有失败都给中文，不透传 `HTTP 409` 这种给人看等于没说的话
 */

import type { TorrentState } from './api'
import { authHeaders, type EngineConfig } from './config'
import { engineMessage } from './engine-error'

export type RowAction = 'pause' | 'resume'
export type ActionKind = RowAction | 'remove'

/** 合法 infohash：40 位 hex（v1）。base32 的 32 位形式引擎不接受删除接口，这里也不放行。 */
const INFOHASH_RE = /^[0-9a-f]{40}$/i

/** 校验并归一化。非法就抛 —— 调用方不该有机会把脏值拼进 URL。 */
export function normalizeInfohash(value: string): string {
  const trimmed = value.trim()
  if (!INFOHASH_RE.test(trimmed)) {
    throw new Error(`任务标识不合法（${trimmed.slice(0, 12)}…），拒绝发送请求`)
  }
  return trimmed.toLowerCase()
}

/** 这一行该显示哪个动作。null = 不给动作按钮。
 *
 * `paused` 优先于 `state`：libtorrent 里状态可能是 `seeding` 而 paused 为 true
 * （用户暂停了一个做种任务），按钮必须显示「继续」而不是「暂停」。
 */
export function rowActionFor(state: TorrentState, paused: boolean): RowAction | null {
  // 两种口径都要认：`state === 'paused'` 是引擎报的状态，`paused` 是独立标志。
  // libtorrent 里「暂停了一个做种任务」就是 seeding + paused=true —— 这时按钮
  // 必须是「继续」，否则点了没有任何反应。反过来若只有状态没有标志（字段降级），
  // 也不能把「已暂停」显示成「暂停」。
  if (paused || state === 'paused') return 'resume'
  switch (state) {
    case 'downloading':
    case 'seeding':
    case 'checking':
    case 'metadata':
      return 'pause'
    // finished 是终态，暂停它没有可见效果（libtorrent 里它可能仍在做种）；
    // queued/allocating/pending 还没真正开始，点了看不出变化；
    // other/unknown 说明状态没认出来，不该给一个可能无效的操作。
    default:
      return null
  }
}

export function describeActionFailure(kind: ActionKind, status: number, body: unknown): string {
  const detail = engineMessage(body)
  if (status === 401 || status === 403) return '引擎拒绝了请求（令牌无效），重启应用试试'
  if (status === 404) {
    return kind === 'remove'
      ? '这个任务已经不在了（可能已被其他程序删除），刷新后就会消失'
      : '引擎找不到这个任务，它可能刚被删除'
  }
  if (status === 409) return detail ?? '这个任务当前状态不允许该操作'
  if (status === 400) return detail ?? '引擎不接受这个请求'
  if (status >= 500) return detail === null ? '引擎处理失败' : `引擎处理失败：${detail}`
  return detail === null ? `操作失败（HTTP ${status}）` : `操作失败（HTTP ${status}）：${detail}`
}

/** 超时上限：本机回环，正常是毫秒级。10s 只用来兜「引擎卡住但端口没关」。 */
const ACTION_TIMEOUT_MS = 10_000

async function call(
  config: EngineConfig,
  kind: ActionKind,
  path: string,
  method: 'POST' | 'DELETE',
  timeoutMs: number,
): Promise<void> {
  const controller = new AbortController()
  const timer = setTimeout(() => {
    controller.abort()
  }, timeoutMs)

  let response: Response
  try {
    response = await fetch(`${config.baseUrl}${path}`, {
      method,
      headers: authHeaders(config),
      signal: controller.signal,
    })
  } catch (error) {
    const aborted = error instanceof Error && error.name === 'AbortError'
    throw new Error(aborted ? '操作超时，引擎没有响应' : '连不上引擎，本地引擎可能没在运行')
  } finally {
    clearTimeout(timer)
  }

  if (response.ok) return
  // 错误体不一定是 JSON（502 之类可能是 HTML），解析失败不该盖掉状态码信息
  const body: unknown = await response.json().catch(() => null)
  throw new Error(describeActionFailure(kind, response.status, body))
}

export async function pauseTorrent(
  config: EngineConfig,
  infohash: string,
  timeoutMs = ACTION_TIMEOUT_MS,
): Promise<void> {
  const ih = normalizeInfohash(infohash)
  await call(config, 'pause', `/torrents/${ih}/pause`, 'POST', timeoutMs)
}

export async function resumeTorrent(
  config: EngineConfig,
  infohash: string,
  timeoutMs = ACTION_TIMEOUT_MS,
): Promise<void> {
  const ih = normalizeInfohash(infohash)
  await call(config, 'resume', `/torrents/${ih}/resume`, 'POST', timeoutMs)
}

/** 删除任务。`deleteFiles` 必须显式给 —— 不依赖引擎默认值，
 * 因为「默认删不删磁盘文件」这件事一旦变了，用户的数据就没了。 */
export async function removeTorrent(
  config: EngineConfig,
  infohash: string,
  deleteFiles: boolean,
  timeoutMs = ACTION_TIMEOUT_MS,
): Promise<void> {
  const ih = normalizeInfohash(infohash)
  const query = deleteFiles ? '?delete_files=1' : '?delete_files=0'
  await call(config, 'remove', `/torrents/${ih}${query}`, 'DELETE', timeoutMs)
}
