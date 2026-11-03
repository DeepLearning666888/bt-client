import { describe, expect, it } from 'vitest'
import { Poller, isUnchangedResponse } from '../src/poller'
import type { Snapshot } from '../src/api'

const fullSnapshot = {
  seq: 5,
  ts_ms: 1000,
  torrent_count: 0,
  torrents: [],
}

function counter<T>() {
  const state = { calls: [] as T[] }
  return {
    state,
    push: (value: T) => {
      state.calls.push(value)
    },
  }
}

describe('isUnchangedResponse', () => {
  it('只认 unchanged === true', () => {
    expect(isUnchangedResponse({ seq: 1, unchanged: true })).toBe(true)
    expect(isUnchangedResponse({ seq: 1, unchanged: false })).toBe(false)
    expect(isUnchangedResponse({ seq: 1 })).toBe(false)
    expect(isUnchangedResponse(null)).toBe(false)
    expect(isUnchangedResponse('unchanged')).toBe(false)
  })
})

describe('Poller 去重', () => {
  it('unchanged 响应下不触发快照回调（这是「不卡」的核心）', async () => {
    const seen = counter<Snapshot>()
    const poller = new Poller(
      async () => ({ seq: 0, unchanged: true }),
      (snapshot) => seen.push(snapshot),
    )

    await poller.tick()
    await poller.tick()
    await poller.tick()

    expect(seen.state.calls).toHaveLength(0)
    expect(poller.stats.unchanged).toBe(3)
    expect(poller.stats.applied).toBe(0)
  })

  it('完整快照只回调一次，后续请求带上新 seq', async () => {
    const seen = counter<Snapshot>()
    const sinceValues: number[] = []
    const poller = new Poller(
      async (since) => {
        sinceValues.push(since)
        return since === 0 ? fullSnapshot : { seq: 5, unchanged: true }
      },
      (snapshot) => seen.push(snapshot),
    )

    await poller.tick()
    await poller.tick()

    expect(seen.state.calls).toHaveLength(1)
    expect(poller.seq).toBe(5)
    // 第一次 0（还不知道），第二次必须带 5
    expect(sinceValues).toEqual([0, 5])
  })

  it('请求抛错 → 走 onError，不向外抛，计入 failures', async () => {
    const errors: unknown[] = []
    const seen = counter<Snapshot>()
    const poller = new Poller(
      async () => {
        throw new Error('ECONNREFUSED')
      },
      (snapshot) => seen.push(snapshot),
      (error) => errors.push(error),
    )

    await expect(poller.tick()).resolves.toBeUndefined()
    expect(errors).toHaveLength(1)
    expect(seen.state.calls).toHaveLength(0)
    expect(poller.stats.failures).toBe(1)
  })

  it('结构不可用 → onError，不产生空快照', async () => {
    const errors: unknown[] = []
    const seen = counter<Snapshot>()
    const poller = new Poller(
      async () => ({ torrents: 'broken' }),
      (snapshot) => seen.push(snapshot),
      (error) => errors.push(error),
    )

    await poller.tick()
    expect(errors).toHaveLength(1)
    expect(seen.state.calls).toHaveLength(0)
  })

  it('上一拍未返回时，重入被忽略（避免请求堆积）', async () => {
    // 用对象持有 resolve：写在闭包里直接赋值会被控制流分析判成 never
    const gate: { release: ((value: unknown) => void) | null } = { release: null }
    let calls = 0
    const poller = new Poller(
      () => {
        calls += 1
        return new Promise<unknown>((resolve) => {
          gate.release = resolve
        })
      },
      () => {},
    )

    const first = poller.tick()
    await poller.tick() // 这一拍应被直接跳过
    expect(calls).toBe(1)

    gate.release?.({ seq: 1, unchanged: true })
    await first
    expect(poller.stats.ticks).toBe(1)
  })
})

describe('Poller 超时（引擎假死）', () => {
  /** 永不 resolve 的 source：模拟「进程还在、但永远不回响应」。
   *  这是杀进程测不出来的形态 —— 端口开着，连接建得上，就是没有数据。 */
  function hung(): (since: number, signal?: AbortSignal) => Promise<unknown> {
    return (_since, signal) =>
      new Promise<unknown>((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new Error('aborted'))
        })
      })
  }

  it('超时后把错误交给 onError，不是无声挂住', async () => {
    const errors: unknown[] = []
    const poller = new Poller(hung(), () => {}, (error) => errors.push(error), 100, 30)

    await poller.tick()

    expect(errors).toHaveLength(1)
    expect((errors[0] as Error).message).toBe('等待引擎响应超时')
    expect(poller.stats.failures).toBe(1)
  })

  it('超时之后 inflight 被释放，下一拍照常发起', async () => {
    // 这条是真正的病灶：没有超时时那一拍永远不结束，inflight 永远为 true，
    // 之后每一拍都被 `if (this.inflight) return` 丢掉 —— 界面永久冻结在
    // 最后一份快照上，且仍显示「在线」。比显示离线更坏。
    let calls = 0
    const poller = new Poller(
      (since, signal) => {
        calls += 1
        if (calls > 1) return Promise.resolve({ seq: 9, torrent_count: 0, torrents: [] })
        return hung()(since, signal)
      },
      () => {},
      () => {},
      100,
      30,
    )

    await poller.tick()
    expect(calls).toBe(1)

    await poller.tick()
    expect(calls).toBe(2)
    expect(poller.stats.applied).toBe(1)
  })

  it('超时会 abort 底层请求，不是只丢下它继续跑', async () => {
    let aborted = false
    const poller = new Poller(
      (_since, signal) => {
        signal?.addEventListener('abort', () => {
          aborted = true
        })
        return new Promise<unknown>(() => {})
      },
      () => {},
      () => {},
      100,
      30,
    )

    await poller.tick()
    expect(aborted).toBe(true)
  })

  it('timeoutMs <= 0 时不设超时（保持旧行为，留给不需要它的调用方）', async () => {
    const poller = new Poller(async () => ({ seq: 1, unchanged: true }), () => {}, () => {}, 100, 0)
    await poller.tick()
    expect(poller.stats.unchanged).toBe(1)
  })
})
