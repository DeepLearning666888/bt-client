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
