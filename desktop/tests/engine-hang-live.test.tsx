/** @vitest-environment jsdom */

/** 引擎假死：端口开着、连接建得上、永远不回数据。
 *
 * 这条与 engine-crash-live 的分界：
 *   - 崩溃（engine-crash-live）：进程没了 → fetch 立刻 reject → 立即转离线。
 *   - 假死（本文件）：进程在、不响应 → 请求挂住 → **只能靠超时**才转离线。
 *
 * 用的是**默认 source（真 fetch）**，不是注入的桩：要验的是「AbortSignal 能不能
 * 真的取消一个真实的 undici 请求」。桩取消了不算数 —— 那种情况下超时只是丢了
 * 一个 Promise，底层 socket 还挂着。
 *
 * 由 desktop/tests/t_engine_hang.py 注入 ENGINE_HANG_URL / ENGINE_HANG_TOKEN。
 * 没有环境变量时跳过，直接 `vitest run` 不会假失败。
 */

import { expect, it } from 'vitest'
import { STATE_TIMEOUT_MS } from '../src/poller'
import { createEngineRuntime } from '../src/runtime'

const baseUrl = process.env['ENGINE_HANG_URL'] ?? ''
const token = process.env['ENGINE_HANG_TOKEN'] ?? ''
const hasHang = baseUrl !== '' && token !== ''

it.skipIf(!hasHang)(
  '假死引擎：请求挂住 → 到点超时 → 中文文案 → 且没有无限等下去',
  async () => {
    // 关掉自动轮询：定时器每 100ms 打一拍，会先占住 inflight，
    // 让手动 tick 被 `if (this.inflight) return` 静默跳过 —— 那样耗时就测不准了。
    const runtime = createEngineRuntime({ baseUrl, token }, { startPolling: false })

    const started = Date.now()
    await runtime.poller!.tick()
    const elapsed = Date.now() - started

    const connection = runtime.store.getConnection()
    expect(connection.status).toBe('offline')
    // 文案必须是中文超时，不能是 undici 的 'The operation was aborted'
    expect(connection.detail).toBe('等待引擎响应超时')

    // 关键：必须真的等到了超时点，而不是碰巧快速失败。
    // 假死服务会一直握着连接，任何「立刻返回」都说明请求根本没发出去。
    expect(elapsed).toBeGreaterThanOrEqual(STATE_TIMEOUT_MS - 250)
    // 也不能远超 —— 那说明 abort 没生效，等到的是别的超时
    expect(elapsed).toBeLessThan(STATE_TIMEOUT_MS + 4000)

    // 超时之后 inflight 必须释放，否则界面永久冻结在最后一份快照上
    const before = runtime.poller!.stats.ticks
    await runtime.poller!.tick()
    expect(runtime.poller!.stats.ticks).toBe(before + 1)

    runtime.dispose()
  },
  STATE_TIMEOUT_MS + 15_000,
)
