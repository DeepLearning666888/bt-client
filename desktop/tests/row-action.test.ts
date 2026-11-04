/** AC-16 / AC-17：任务操作的请求契约与「该显示哪个按钮」的决策。
 *
 * 两条都在纯函数 + stub fetch 层断言：这几件事一旦失败，说明「发出去的东西不对」
 * 或「按钮给错了」，都不需要界面在场才能发现。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { KNOWN_STATES, type TorrentState } from '../src/api'
import {
  describeActionFailure,
  normalizeInfohash,
  pauseTorrent,
  removeTorrent,
  resumeTorrent,
  rowActionFor,
} from '../src/torrent-action'

const IH = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
const CONFIG = { baseUrl: 'http://127.0.0.1:2950', token: 'tk-test' }

function stubFetch(status = 200, body = '{"ok":true}') {
  const calls: Array<{ url: string; init: RequestInit }> = []
  vi.stubGlobal('fetch', (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return Promise.resolve(
      new Response(body, { status, headers: { 'Content-Type': 'application/json' } }),
    )
  })
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('normalizeInfohash', () => {
  it('接受 40 位 hex 并转小写', () => {
    expect(normalizeInfohash(IH.toUpperCase())).toBe(IH)
    expect(normalizeInfohash(`  ${IH}  `)).toBe(IH)
  })

  it('拒绝把别的路径片段拼进 URL', () => {
    // 这一条是安全断言：这个值来自引擎响应、会被拼进路径，
    // 一旦上游被污染，不校验就等于替任何人发请求
    for (const bad of ['../shutdown', 'a/b', `${IH}/pause`, '', 'abc']) {
      expect(() => normalizeInfohash(bad)).toThrow('不合法')
    }
  })

  it('拒绝 32 位 base32（删除接口只认 hex）', () => {
    expect(() => normalizeInfohash('abcdefghijklmnopqrstuvwxyz234567')).toThrow('不合法')
  })
})

describe('rowActionFor', () => {
  it('下载/做种/校验/取元数据时显示暂停', () => {
    for (const state of ['downloading', 'seeding', 'checking', 'metadata'] as const) {
      expect(rowActionFor(state, false)).toBe('pause')
    }
  })

  it('已暂停时显示继续', () => {
    expect(rowActionFor('paused', true)).toBe('resume')
  })

  it('paused 标志与状态矛盾时以 paused 为准', () => {
    // libtorrent 里「暂停了一个做种任务」就是 seeding + paused=true，
    // 这时按钮必须是「继续」，否则点了没反应
    expect(rowActionFor('seeding', true)).toBe('resume')
    expect(rowActionFor('downloading', true)).toBe('resume')
  })

  it('终态与未开始的状态不给动作', () => {
    for (const state of ['finished', 'queued', 'allocating', 'pending', 'other', 'unknown'] as const) {
      expect(rowActionFor(state, false)).toBeNull()
    }
  })

  it('全部状态逐个对照期望表（新增状态时 TS 会在这里报错）', () => {
    // 用 Record<TorrentState, …> 而不是数组：给 TorrentState 加一个值时，
    // 这张表少写一行就编译不过，比运行时断言更早拦住
    const expected: Record<TorrentState, ReturnType<typeof rowActionFor>> = {
      downloading: 'pause',
      seeding: 'pause',
      checking: 'pause',
      metadata: 'pause',
      paused: 'resume',
      finished: null,
      queued: null,
      allocating: null,
      pending: null,
      other: null,
      unknown: null,
    }
    for (const [state, action] of Object.entries(expected)) {
      expect(rowActionFor(state as TorrentState, false)).toBe(action)
    }
    // KNOWN_STATES 的每一项都必须在这张表里被显式分类过
    for (const state of KNOWN_STATES) {
      expect(Object.keys(expected)).toContain(state)
    }
  })
})

describe('describeActionFailure', () => {
  const body = (message: string) => ({ error: { code: 'x_failed', message } })

  it('删除时 404 说「已经不在了」，不说「找不到接口」', () => {
    expect(describeActionFailure('remove', 404, null)).toContain('已经不在了')
  })

  it('暂停时 404 说「找不到这个任务」', () => {
    expect(describeActionFailure('pause', 404, null)).toContain('找不到')
  })

  it('401/403 指向令牌', () => {
    expect(describeActionFailure('resume', 401, null)).toContain('令牌')
    expect(describeActionFailure('remove', 403, null)).toContain('令牌')
  })

  it('409 透传引擎原因，没有原因时给状态类兜底', () => {
    expect(describeActionFailure('pause', 409, body('任务当前不可暂停'))).toBe('任务当前不可暂停')
    expect(describeActionFailure('pause', 409, null)).toContain('不允许')
  })

  it('5xx 标注为引擎处理失败', () => {
    expect(describeActionFailure('remove', 500, body('磁盘忙'))).toBe('引擎处理失败：磁盘忙')
    expect(describeActionFailure('remove', 500, null)).toBe('引擎处理失败')
  })
})

describe('操作请求契约', () => {
  it('暂停走 POST /torrents/<ih>/pause，不带 body', async () => {
    const calls = stubFetch()
    await pauseTorrent(CONFIG, IH)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(`http://127.0.0.1:2950/torrents/${IH}/pause`)
    expect(calls[0]!.init.method).toBe('POST')
    expect((calls[0]!.init.headers as Record<string, string>)['Authorization']).toBe('Bearer tk-test')
    expect(calls[0]!.init.body).toBeUndefined()
  })

  it('恢复走 POST /torrents/<ih>/resume', async () => {
    const calls = stubFetch()
    await resumeTorrent(CONFIG, IH)
    expect(calls[0]!.url).toBe(`http://127.0.0.1:2950/torrents/${IH}/resume`)
    expect(calls[0]!.init.method).toBe('POST')
  })

  it('删除显式传 delete_files=1，且用 DELETE', async () => {
    const calls = stubFetch()
    await removeTorrent(CONFIG, IH, true)
    expect(calls[0]!.url).toBe(`http://127.0.0.1:2950/torrents/${IH}?delete_files=1`)
    expect(calls[0]!.init.method).toBe('DELETE')
  })

  it('不删文件时传 delete_files=0，不省略参数', async () => {
    // 省略参数就是在依赖引擎默认值 —— 默认值一变，用户数据就没了
    const calls = stubFetch()
    await removeTorrent(CONFIG, IH, false)
    expect(calls[0]!.url).toBe(`http://127.0.0.1:2950/torrents/${IH}?delete_files=0`)
  })

  it('infohash 非法时不发请求', async () => {
    const calls = stubFetch()
    await expect(pauseTorrent(CONFIG, 'nope')).rejects.toThrow('不合法')
    await expect(removeTorrent(CONFIG, '../shutdown', false)).rejects.toThrow('不合法')
    expect(calls).toHaveLength(0)
  })

  it('非 2xx 抛中文文案（409 走任务状态解释）', async () => {
    stubFetch(409, JSON.stringify({ error: { code: 'x', message: '任务当前不可暂停' } }))
    await expect(pauseTorrent(CONFIG, IH)).rejects.toThrow('任务当前不可暂停')
  })

  it('网络异常与超时各有独立文案', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))
    await expect(pauseTorrent(CONFIG, IH)).rejects.toThrow('连不上引擎')

    vi.stubGlobal('fetch', () => {
      const error = new Error('aborted')
      error.name = 'AbortError'
      return Promise.reject(error)
    })
    await expect(removeTorrent(CONFIG, IH, false)).rejects.toThrow('超时')
  })
})

describe('删除的部分完成（任务删了但文件没删掉）', () => {
  it('响应带 warning 时作为返回值交给调用方，不当成失败', async () => {
    // 引擎实测：文件被别的程序占着时它删不掉，但任务确实移除了。
    // 这里返回 200 + warning。当失败抛出去会让用户以为任务还在，
    // 静默吞掉会让用户以为磁盘空间回来了 —— 都不对，得原样往上传。
    stubFetch(200, JSON.stringify({ removed: IH, warning: '文件没能删掉，可能正被其他程序占用：C://x.bin' }))
    const warning = await removeTorrent(CONFIG, IH, true)
    expect(warning).toBe('文件没能删掉，可能正被其他程序占用：C://x.bin')
  })

  it('没有 warning 字段时返回 null', async () => {
    stubFetch(200, JSON.stringify({ removed: IH }))
    expect(await removeTorrent(CONFIG, IH, true)).toBeNull()
  })

  it('warning 是空串或非字符串时按「没有」处理', async () => {
    for (const body of [
      JSON.stringify({ removed: IH, warning: '' }),
      JSON.stringify({ removed: IH, warning: '   ' }),
      JSON.stringify({ removed: IH, warning: 42 }),
      JSON.stringify({ removed: IH, warning: null }),
    ]) {
      stubFetch(200, body)
      expect(await removeTorrent(CONFIG, IH, true)).toBeNull()
    }
  })

  it('响应体不是 JSON 时也不炸，按干净完成处理', async () => {
    // 正常情况引擎给 JSON；但中间有代理、或未来改了响应格式时，
    // 「删成功了但读不出警告」不该被当成删除失败
    stubFetch(200, 'not json at all')
    expect(await removeTorrent(CONFIG, IH, true)).toBeNull()
  })

  it('暂停/继续没有这个返回值，仍然是 void', async () => {
    stubFetch(200, JSON.stringify({ ok: true }))
    // 类型上就是 void，这里断言运行时也不会漏出内容
    expect(await pauseTorrent(CONFIG, IH)).toBeUndefined()
    expect(await resumeTorrent(CONFIG, IH)).toBeUndefined()
  })
})
