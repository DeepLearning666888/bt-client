/** 10Hz 轮询 + seq 去重。
 *
 * 为什么这是「界面不卡」的地基：每秒 10 次把整份快照塞进 setState，会让整棵树
 * 重渲；任务一多，重渲本身就成了瓶颈。引擎侧已经支持 `?since=N`，seq 未变时只回
 * `{"seq":N,"unchanged":true}`。此时必须做到「零回调」——不是回调之后在组件里
 * 判断相等，而是根本不产生新对象。
 */

import { parseSnapshot, type Snapshot } from './api'

export type SnapshotSource = (since: number) => Promise<unknown>

export function isUnchangedResponse(raw: unknown): boolean {
  return (
    typeof raw === 'object' &&
    raw !== null &&
    (raw as Record<string, unknown>)['unchanged'] === true
  )
}

export interface PollerStats {
  ticks: number
  unchanged: number
  applied: number
  failures: number
}

export class Poller {
  private lastSeq = 0
  private inflight = false
  private stopped = false
  private timer: ReturnType<typeof setInterval> | null = null
  readonly stats: PollerStats = { ticks: 0, unchanged: 0, applied: 0, failures: 0 }

  constructor(
    private readonly source: SnapshotSource,
    private readonly onSnapshot: (snapshot: Snapshot) => void,
    private readonly onError: (error: unknown) => void = () => {},
    private readonly intervalMs = 100,
  ) {}

  /** 单次轮询。刻意做成公开方法：定时器不该是测试的前提。 */
  async tick(): Promise<void> {
    // 上一次还没回来就跳过这一拍，避免请求堆积把引擎打满
    if (this.inflight || this.stopped) return
    this.inflight = true
    try {
      this.stats.ticks += 1
      const raw = await this.source(this.lastSeq)

      if (isUnchangedResponse(raw)) {
        this.stats.unchanged += 1
        return
      }

      const snapshot = parseSnapshot(raw)
      if (snapshot === null) {
        this.stats.failures += 1
        this.onError(new Error('快照结构不可用'))
        return
      }

      this.lastSeq = snapshot.seq
      this.stats.applied += 1
      this.onSnapshot(snapshot)
    } catch (error) {
      this.stats.failures += 1
      this.onError(error)
    } finally {
      this.inflight = false
    }
  }

  start(): void {
    if (this.timer !== null || this.stopped) return
    this.timer = setInterval(() => {
      void this.tick()
    }, this.intervalMs)
  }

  stop(): void {
    this.stopped = true
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
  }

  get seq(): number {
    return this.lastSeq
  }
}
