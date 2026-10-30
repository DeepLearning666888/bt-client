/** 引擎 JSON → UI 模型的解析层。
 *
 * 引擎返回的 JSON 是不可信输入：字段可能缺失、类型可能不符、磁力任务可能还没有
 * 元数据。这里的原则是「字段级降级，整体不崩」：
 *   - 结构都不对（非对象、torrents 不是数组）→ 返回 null，调用方显示离线
 *   - 单个字段坏了 → 该字段降级为安全值，并在快照上标记 degraded
 * 不用 zod：多一个依赖只为十几行校验不划算，而且运行时库在打包时无法 tree-shake 掉。
 */

export type TorrentState =
  | 'downloading'
  | 'seeding'
  | 'paused'
  | 'checking'
  | 'metadata'
  | 'finished'
  | 'queued'
  | 'allocating'
  | 'pending'
  | 'other'
  | 'unknown'

/** 引擎可能吐出的全部状态。
 *
 * 这份集合必须覆盖 `engine/src/engine.cpp` 里 `state_name()` 的返回值，外加
 * `rebuild_state()` 在拿到 handle 之前用的 `pending`。漏一个的后果是真实任务被
 * 当成 `unknown`、并且整份快照被标成 degraded —— 界面上就是一条黄色警告加
 * 一个看不懂的 `unknown`。磁力任务刚添加时正好停在 `metadata`，所以漏了它必然
 * 每次都触发。
 *
 * 两边靠 `tests/state-vocab.test.ts` 从引擎源码里抓字面量交叉校验，不靠自觉。
 */
export const KNOWN_STATES: readonly TorrentState[] = [
  'downloading',
  'seeding',
  'paused',
  'checking',
  'metadata',
  'finished',
  'queued',
  'allocating',
  'pending',
  'other',
]

export interface Torrent {
  infohash: string
  name: string
  state: TorrentState
  paused: boolean
  /** 0..1 */
  progress: number
  rateDown: number
  rateUp: number
  peers: number
  seeds: number
  doneBytes: number
  totalBytes: number
  etaS: number | null
  hasMetadata: boolean
  error: string | null
}

export interface Snapshot {
  seq: number
  tsMs: number
  count: number
  torrents: Torrent[]
  /** 有字段被降级时为 true，UI 可以据此提示「数据不完整」 */
  degraded: boolean
}

function asObject(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function asInt(value: unknown, fallback: number): number {
  return Math.trunc(asNumber(value, fallback))
}

function asBool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

function asString(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0
  if (value < 0) return 0
  if (value > 1) return 1
  return value
}

function asState(value: unknown): TorrentState {
  if (typeof value !== 'string') return 'unknown'
  return (KNOWN_STATES as readonly string[]).includes(value) ? (value as TorrentState) : 'unknown'
}

/** 解析单个任务。没有 infohash 就没有身份，直接丢弃 —— 用它做 key 会撞车。 */
export function parseTorrent(raw: unknown): { torrent: Torrent; degraded: boolean } | null {
  const o = asObject(raw)
  if (o === null) return null

  const infohash = asString(o['infohash'], '')
  if (infohash.length === 0) return null

  const hasMetadata = asBool(o['has_metadata'], false)
  const rawName = asString(o['name'], '').trim()
  // 无元数据的磁力没有名字，用 infohash 前缀兜底，至少能区分是哪个任务
  const name = rawName.length > 0 ? rawName : `${hasMetadata ? '未知任务' : '磁力'} ${infohash.slice(0, 8)}`

  const rawProgress = o['progress']
  const progress = clamp01(asNumber(rawProgress, 0))

  const etaRaw = o['eta_s']
  const etaS = typeof etaRaw === 'number' && Number.isFinite(etaRaw) && etaRaw >= 0 ? etaRaw : null

  const errRaw = o['error']
  const error = typeof errRaw === 'string' && errRaw.length > 0 ? errRaw : null

  const torrent: Torrent = {
    infohash,
    name,
    state: asState(o['state']),
    paused: asBool(o['paused'], false),
    progress,
    rateDown: asInt(o['rate_down'], 0),
    rateUp: asInt(o['rate_up'], 0),
    peers: asInt(o['peers'], 0),
    seeds: asInt(o['seeds'], 0),
    doneBytes: asInt(o['done_bytes'], 0),
    totalBytes: asInt(o['total_bytes'], 0),
    etaS,
    hasMetadata,
    error,
  }

  // 判定「有没有字段被降级」：任何一处类型不对或状态不认识都算
  const degraded =
    typeof rawProgress !== 'number' ||
    typeof o['rate_down'] !== 'number' ||
    typeof o['state'] !== 'string' ||
    torrent.state === 'unknown' ||
    typeof o['name'] !== 'string' ||
    typeof o['has_metadata'] !== 'boolean'

  return { torrent, degraded }
}

/** 解析整份快照。结构不可用时返回 null，由调用方按离线处理。 */
export function parseSnapshot(raw: unknown): Snapshot | null {
  const o = asObject(raw)
  if (o === null) return null

  const list = o['torrents']
  if (!Array.isArray(list)) return null

  let degraded = typeof o['seq'] !== 'number'

  const torrents: Torrent[] = []
  for (const item of list) {
    const parsed = parseTorrent(item)
    if (parsed === null) {
      degraded = true
      continue
    }
    if (parsed.degraded) degraded = true
    torrents.push(parsed.torrent)
  }

  const declaredCount = asInt(o['torrent_count'], torrents.length)
  if (declaredCount !== list.length) degraded = true

  return {
    seq: asInt(o['seq'], 0),
    tsMs: asInt(o['ts_ms'], 0),
    count: torrents.length,
    torrents,
    degraded,
  }
}
