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
import { engineMessage } from './engine-error'

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
}/** 超时上限。引擎在本机回环，正常是毫秒级；15s 只用来兜「引擎没响应但端口没关」。 */
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

/* ------------------------------ .torrent 文件 ------------------------------ */

/** 引擎 HTTP 层的请求体上限（`engine/src/http.cpp:321`，超了直接 413 且不进解析）。
 * 两边改动必须同步；前端先拦是为了给出带数值的文案。 */
export const TORRENT_MAX_BYTES = 8 * 1024 * 1024

/** 只依赖 name / size，所以 `File` 和测试里的假对象都能传。 */
export interface FileLike {
  name: string
  size: number
}

export type FileVerdict = { ok: true } | { ok: false; message: string }

function formatMiB(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

/** 发送前的同步校验：扩展名、空文件、上限。
 *
 * 刻意**不**校验内容 —— 那要读字节，得异步；而且真正的解析器在引擎里，
 * 它给的原话比我猜的准。这里只拦「不用打开就知道不行」的三类。 */
export function validateTorrentFile(file: FileLike): FileVerdict {
  if (!file.name.toLowerCase().endsWith('.torrent')) {
    return { ok: false, message: `这不是 .torrent 文件（${file.name}）` }
  }
  if (file.size === 0) {
    return { ok: false, message: '文件是空的（0 字节）' }
  }
  if (file.size > TORRENT_MAX_BYTES) {
    return {
      ok: false,
      message: `文件 ${formatMiB(file.size)}，超过 ${formatMiB(TORRENT_MAX_BYTES)} 上限`,
    }
  }
  return { ok: true }
}

/** 种子文件是 bencode 字典，首字节必须是 `d`。
 *
 * 容忍前导空白与 UTF-8 BOM：有些客户端导出的文件带 BOM，
 * 引擎那边能解析，这里不该提前拦。改过后缀的 zip / 文本在这里被拦下。 */
export function looksLikeTorrent(bytes: Uint8Array): boolean {
  let i = 0
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    i = 3
  }
  while (i < bytes.length) {
    const b = bytes[i]
    if (b !== 0x20 && b !== 0x09 && b !== 0x0a && b !== 0x0d) break
    i += 1
  }
  return i < bytes.length && bytes[i] === 0x64 // 'd'
}

/** 校验 + 读字节的合成步骤。放在这里而不是组件里：读文件是异步的，
 * 放在组件里会让「什么算合法」散成 setState 序列，测试只能靠点界面。 */
export interface FileBytes {
  name: string
  size: number
  arrayBuffer(): Promise<ArrayBuffer>
}

// 用 Uint8Array<ArrayBuffer> 而不是裸 Uint8Array：后者默认 ArrayBufferLike，
// 含 SharedArrayBuffer 的可能，fetch 的 BodyInit 不接受。
export type TorrentBytes = Uint8Array<ArrayBuffer>

export type PreparedFile =
  | { ok: true; bytes: TorrentBytes; name: string }
  | { ok: false; message: string }

export async function prepareTorrentFile(file: FileBytes): Promise<PreparedFile> {
  const verdict = validateTorrentFile(file)
  if (!verdict.ok) return verdict

  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  if (bytes.length === 0) return { ok: false, message: '文件是空的（0 字节）' }
  if (!looksLikeTorrent(bytes)) {
    return { ok: false, message: '这个文件不像种子（内容开头不是种子格式），可能只是改了后缀名' }
  }
  return { ok: true, bytes, name: file.name }
}

/** 文件模式的上传。与磁力模式的差别只有 content-type 和 body 形状，其余共用。 */
export async function addTorrentFile(
  config: EngineConfig,
  bytes: TorrentBytes,
  timeoutMs = ADD_TIMEOUT_MS,
): Promise<AddOutcome> {
  if (bytes.length === 0) throw new Error('文件是空的（0 字节）')

  const controller = new AbortController()
  const timer = setTimeout(() => {
    controller.abort()
  }, timeoutMs)

  let response: Response
  try {
    response = await fetch(`${config.baseUrl}/torrents`, {
      method: 'POST',
      headers: { ...authHeaders(config), 'Content-Type': 'application/octet-stream' },
      body: bytes,
      signal: controller.signal,
    })
  } catch (error) {
    const aborted = error instanceof Error && error.name === 'AbortError'
    throw new Error(aborted ? '等待引擎响应超时' : '连不上引擎，本地引擎可能没在运行')
  } finally {
    clearTimeout(timer)
  }

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
