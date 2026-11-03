/** AC-09 / AC-10：磁力校验、错误文案、请求契约。
 *
 * 全部在纯函数 + stub fetch 层断言，不起 DOM、不起引擎 —— 这几条一旦失败，
 * 说明「发出去的东西不对」或「回来的错误说不清」，两者都不需要界面在场才能发现。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { addTorrent, describeAddFailure, normalizeMagnet } from '../src/add-task'

const IH = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
const MAGNET = `magnet:?xt=urn:btih:${IH}&dn=demo`

const CONFIG = { baseUrl: 'http://127.0.0.1:2950', token: 'tk-test' }

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('normalizeMagnet', () => {
  it('拒绝空串与纯空白', () => {
    for (const input of ['', '   ', '\n\t']) {
      const verdict = normalizeMagnet(input)
      expect(verdict.ok).toBe(false)
      if (!verdict.ok) expect(verdict.message).toContain('请粘贴')
    }
  })

  it('接受标准磁力并去掉首尾空白', () => {
    const verdict = normalizeMagnet(`  ${MAGNET}\n`)
    expect(verdict).toEqual({ ok: true, magnet: MAGNET })
  })

  it('协议与参数名大小写不敏感', () => {
    const upper = `MAGNET:?XT=URN:BTIH:${IH.toUpperCase()}`
    expect(normalizeMagnet(upper).ok).toBe(true)
  })

  it('接受 32 位 base32 形式的 btih', () => {
    const base32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    expect(base32).toHaveLength(32)
    expect(normalizeMagnet(`magnet:?xt=urn:btih:${base32}`).ok).toBe(true)
  })

  it('拒绝缺 btih 的 magnet', () => {
    const verdict = normalizeMagnet('magnet:?dn=only-name')
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.message).toContain('btih')
  })

  it('拒绝被截断的 hash', () => {
    expect(normalizeMagnet('magnet:?xt=urn:btih:abc123').ok).toBe(false)
  })

  it('粘成网页地址时给出针对性提示，而不是笼统的「格式错误」', () => {
    for (const url of ['https://example.com/a.torrent', 'http://example.com/a.torrent']) {
      const verdict = normalizeMagnet(url)
      expect(verdict.ok).toBe(false)
      if (!verdict.ok) expect(verdict.message).toContain('网页地址')
    }
  })

  it('普通文字不算磁力', () => {
    const verdict = normalizeMagnet('随便粘的一段话')
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.message).toContain('magnet:?')
  })
})

describe('describeAddFailure', () => {
  const body = (message: string) => ({ error: { code: 'add_failed', message } })

  it('409 说成「已经在列表里」，不透传引擎的 conflict 前缀', () => {
    expect(describeAddFailure(409, body('该任务已存在'))).toBe('这个任务已经在列表里了')
  })

  it('401/403 指向令牌而不是磁力', () => {
    expect(describeAddFailure(401, null)).toContain('令牌')
    expect(describeAddFailure(403, null)).toContain('令牌')
  })

  it('400 透传引擎给的中文原因', () => {
    expect(describeAddFailure(400, body('磁力链接缺少有效的 btih')))
      .toBe('磁力链接缺少有效的 btih')
  })

  it('400 没有原因时有兜底文案', () => {
    expect(describeAddFailure(400, null)).toContain('不合法')
  })

  it('5xx 标注为引擎处理失败并带原因', () => {
    expect(describeAddFailure(500, body('内部错误'))).toBe('引擎处理失败：内部错误')
    expect(describeAddFailure(500, {})).toBe('引擎处理失败')
  })

  it('404 提示版本不匹配', () => {
    expect(describeAddFailure(404, null)).toContain('版本')
  })

  it('未知状态码保留原始码，便于排查', () => {
    expect(describeAddFailure(418, null)).toContain('418')
  })
})

describe('addTorrent 请求契约', () => {
  it('POST <base>/torrents，带 Bearer 与 JSON body，返回 infohash', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(
        new Response(JSON.stringify({ infohash: IH }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })

    const outcome = await addTorrent(CONFIG, MAGNET)

    expect(outcome.infohash).toBe(IH)
    expect(calls).toHaveLength(1)
    const call = calls[0]!
    expect(call.url).toBe('http://127.0.0.1:2950/torrents')
    expect(call.init.method).toBe('POST')
    const headers = call.init.headers as Record<string, string>
    expect(headers['Authorization']).toBe('Bearer tk-test')
    expect(headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(call.init.body))).toEqual({ magnet: MAGNET })
  })

  it('非 2xx 抛出带中文文案的 Error', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { code: 'add_failed', message: '该任务已存在' } }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    await expect(addTorrent(CONFIG, MAGNET)).rejects.toThrow('这个任务已经在列表里了')
  })

  it('响应体不是 JSON 时也不丢状态码信息', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(new Response('<html>bad gateway</html>', { status: 502 })),
    )
    await expect(addTorrent(CONFIG, MAGNET)).rejects.toThrow(/502|引擎处理失败/)
  })

  it('200 但缺 infohash 时报错，不返回空对象', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      ),
    )
    await expect(addTorrent(CONFIG, MAGNET)).rejects.toThrow('infohash')
  })

  it('网络异常映射成「连不上引擎」', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))
    await expect(addTorrent(CONFIG, MAGNET)).rejects.toThrow('连不上引擎')
  })

  it('超时（AbortError）有独立文案', async () => {
    vi.stubGlobal('fetch', () => {
      const error = new Error('aborted')
      error.name = 'AbortError'
      return Promise.reject(error)
    })
    await expect(addTorrent(CONFIG, MAGNET)).rejects.toThrow('超时')
  })
})
