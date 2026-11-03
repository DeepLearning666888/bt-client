/** 10Hz 轮询 + seq 去重。
 *
 * 为什么这是「界面不卡」的地基：每秒 10 次把整份快照塞进 setState，会让整棵树
 * 重渲；任务一多，重渲本身就成了瓶颈。引擎侧已经支持 `?since=N`，seq 未变时只回
 * `{"seq":N,"unchanged":true}`。此时必须做到「零回调」——不是回调之后在组件里
 * 判断相等，而是根本不产生新对象。
 */

import { parseSnapshot, type Snapshot } from './api'

export type SnapshotSource = (since: number, signal?: AbortSignal) => Promise<unknown>

/** 单次轮询的等待上限。
 *
 * 引擎在本机回环，正常是毫秒级。这个值只兜一种情况：**进程还在、但不响应**
 * （假死、被调试器挂住、在做超长阻塞操作）。没有它的话，那一拍会永远挂着，
 * `inflight` 永不释放 —— 之后每一拍都在 `if (this.inflight) return` 处被丢掉，
 * 界面永远停在最后一份快照上，还显示「在线」。那比显示离线更坏。
 *
 * 比「添加任务」的 15s 短：轮询是高频动作，卡 3 秒已经丢掉 30 拍，
 * 再等下去没有意义。
 */
export const STATE_TIMEOUT_MS = 3000

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
    private readonly timeoutMs = STATE_TIMEOUT_MS,
  ) {}

  /** 单次轮询。刻意做成公开方法：定时器不该是测试的前提。 */
  async tick(): Promise<void> {
    // 上一次还没回来就跳过这一拍，避免请求堆积把引擎打满
    if (this.inflight || this.stopped) return
    this.inflight = true
    // 超时用 abort 而不是只 race：race 只是「我不等了」，底层请求还占着连接，
    // 引擎真死时会攒下一堆挂着的 socket。
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | null = null
    // 文案由这个标志决定，而不是由 race 收到哪个 reject 决定。
    // abort() 会同步触发 source 自身的 reject（fetch 抛 AbortError），
    // 它比我们自己的超时 reject 更早到达 race —— 直接透传的话，用户看到的是
    // 一句英文技术错误，而不是「等待引擎响应超时」。
    let timedOut = false
    try {
      this.stats.ticks += 1

      const request = this.source(this.lastSeq, controller.signal)
      const raw =
        this.timeoutMs > 0
          ? await Promise.race([
              request,
              new Promise<never>((_, reject) => {
                timer = setTimeout(() => {
                  timedOut = true
                  controller.abort()
                  reject(new Error('等待引擎响应超时'))
                }, this.timeoutMs)
              }),
            ])
          : await request

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
      this.onError(timedOut ? new Error('等待引擎响应超时') : error)
    } finally {
      // 不 clearTimeout 的话，成功返回后定时器还会在未来某一刻触发一次 abort，
      // 而那时 controller 已经用不上了 —— 是一个纯粹的泄漏（也是测试里的 open handle）。
      if (timer !== null) clearTimeout(timer)
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
