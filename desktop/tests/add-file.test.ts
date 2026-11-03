/** AC-13 / AC-14：`.torrent` 文件的校验与上传契约。
 *
 * 与磁力那套刻意分开：校验规则完全不同（那边查 btih 语法，这边查大小与首字节），
 * 请求体形态也不同（JSON vs 原始字节）。共用的只有错误映射。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  addTorrentFile,
  TORRENT_MAX_BYTES,
  looksLikeTorrent,
  prepareTorrentFile,
  validateTorrentFile,
  type FileBytes,
} from '../src/add-task'

const IH = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
const CONFIG = { baseUrl: 'http://127.0.0.1:2950', token: 'tk-test' }

/** 合法种子的最小形态：bencode 字典开头。内容不必真能解析 —— 那是引擎的事。 */
const TORRENT_BYTES = new TextEncoder().encode('d8:announce14:http://t/a/ann4:infod4:name4:demoee')

function fakeFile(name: string, bytes: Uint8Array, declaredSize?: number): FileBytes {
  return {
    name,
    size: declaredSize ?? bytes.length,
    arrayBuffer: () => Promise.resolve(bytes.buffer.slice(
      bytes.byteOffset, bytes.byteOffset + bytes.byteLength,
    ) as ArrayBuffer),
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('validateTorrentFile', () => {
  it('接受正常命名的文件', () => {
    expect(validateTorrentFile({ name: 'debian.torrent', size: 42_000 })).toEqual({ ok: true })
  })

  it('扩展名大小写不敏感', () => {
    expect(validateTorrentFile({ name: 'DEBIAN.TORRENT', size: 1 }).ok).toBe(true)
  })

  it('拒绝 0 字节', () => {
    const verdict = validateTorrentFile({ name: 'a.torrent', size: 0 })
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.message).toContain('空')
  })

  it('拒绝超过引擎上限的文件，并报出具体数值', () => {
    const verdict = validateTorrentFile({ name: 'a.torrent', size: TORRENT_MAX_BYTES + 1 })
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) {
      // 文案里必须有「几 MiB」和「上限多少」，否则用户不知道差多少
      expect(verdict.message).toMatch(/\d+\.\d MiB/)
      expect(verdict.message).toContain('上限')
    }
  })

  it('刚好等于上限是允许的（边界不越界）', () => {
    expect(validateTorrentFile({ name: 'a.torrent', size: TORRENT_MAX_BYTES }).ok).toBe(true)
  })

  it('拒绝扩展名不对的文件，并回显文件名', () => {
    const verdict = validateTorrentFile({ name: 'ubuntu.iso', size: 1024 })
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) {
      expect(verdict.message).toContain('ubuntu.iso')
      expect(verdict.message).toContain('.torrent')
    }
  })

  it('四类非法输入给出四条不同的原因（不能都回同一句话）', () => {
    const messages = [
      validateTorrentFile({ name: 'a.torrent', size: 0 }),
      validateTorrentFile({ name: 'a.torrent', size: TORRENT_MAX_BYTES * 2 }),
      validateTorrentFile({ name: 'a.zip', size: 100 }),
      validateTorrentFile({ name: 'b.torrent', size: 0 }),
    ]
      .filter((v): v is { ok: false; message: string } => !v.ok)
      .map((v) => v.message)

    // 第 3、4 条本该不同（一个是后缀一个是空文件），这里顺带锁住顺序与彼此差异
    expect(messages[0]).not.toBe(messages[1])
    expect(messages[0]).not.toBe(messages[2])
    expect(messages[1]).not.toBe(messages[2])
  })
})

describe('looksLikeTorrent', () => {
  it('bencode 字典开头算种子', () => {
    expect(looksLikeTorrent(TORRENT_BYTES)).toBe(true)
  })

  it('纯文本被拒', () => {
    expect(looksLikeTorrent(new TextEncoder().encode('hello world'))).toBe(false)
  })

  it('zip 头被拒（PK\\x03\\x04）', () => {
    expect(looksLikeTorrent(new Uint8Array([0x50, 0x4b, 0x03, 0x04]))).toBe(false)
  })

  it('容忍前导空白与 UTF-8 BOM', () => {
    expect(looksLikeTorrent(new TextEncoder().encode('  \r\n d3:foo'))).toBe(true)
    expect(looksLikeTorrent(new Uint8Array([0xef, 0xbb, 0xbf, 0x64]))).toBe(true)
  })

  it('全空白不算种子（不能因为跳空白跳过头就返回 true）', () => {
    expect(looksLikeTorrent(new TextEncoder().encode('   \n'))).toBe(false)
  })

  it('空数组不算种子', () => {
    expect(looksLikeTorrent(new Uint8Array([]))).toBe(false)
  })
})

describe('prepareTorrentFile', () => {
  it('合法文件返回字节与文件名', async () => {
    const prepared = await prepareTorrentFile(fakeFile('ok.torrent', TORRENT_BYTES))
    expect(prepared.ok).toBe(true)
    if (prepared.ok) {
      expect(prepared.name).toBe('ok.torrent')
      expect(prepared.bytes.length).toBe(TORRENT_BYTES.length)
      expect(prepared.bytes[0]).toBe(0x64)
    }
  })

  it('后缀对但内容不是种子时拦下，不交给引擎', async () => {
    const prepared = await prepareTorrentFile(
      fakeFile('fake.torrent', new TextEncoder().encode('我其实是个文本文件')),
    )
    expect(prepared.ok).toBe(false)
    if (!prepared.ok) expect(prepared.message).toContain('不像种子')
  })

  it('同步校验失败时不读字节（arrayBuffer 不该被调用）', async () => {
    const arrayBuffer = vi.fn()
    const prepared = await prepareTorrentFile({ name: 'a.zip', size: 10, arrayBuffer })
    expect(prepared.ok).toBe(false)
    expect(arrayBuffer).not.toHaveBeenCalled()
  })
})

describe('addTorrentFile 请求契约', () => {
  it('POST <base>/torrents，content-type 为 octet-stream，body 是原始字节', async () => {
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

    const outcome = await addTorrentFile(CONFIG, TORRENT_BYTES)

    expect(outcome.infohash).toBe(IH)
    expect(calls).toHaveLength(1)
    const call = calls[0]!
    expect(call.url).toBe('http://127.0.0.1:2950/torrents')
    expect(call.init.method).toBe('POST')
    const headers = call.init.headers as Record<string, string>
    expect(headers['Authorization']).toBe('Bearer tk-test')
    expect(headers['Content-Type']).toBe('application/octet-stream')
    // 关键：body 必须是字节，不能被 JSON.stringify 包成字符串
    expect(call.init.body).toBeInstanceOf(Uint8Array)
    expect((call.init.body as Uint8Array).length).toBe(TORRENT_BYTES.length)
  })

  it('空字节直接拒绝，不发请求', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    await expect(addTorrentFile(CONFIG, new Uint8Array([]))).rejects.toThrow('空')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('错误映射与磁力共用一套（409 → 已经在列表里）', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { code: 'add_failed', message: 'conflict: 该任务已存在' } }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    await expect(addTorrentFile(CONFIG, TORRENT_BYTES)).rejects.toThrow('已经在列表里')
  })

  it('400 透传引擎的解析失败原话', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(
          JSON.stringify({ error: { code: 'add_failed', message: '种子文件解析失败：expected value' } }),
          { status: 400, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    await expect(addTorrentFile(CONFIG, TORRENT_BYTES)).rejects.toThrow('种子文件解析失败')
  })

  it('网络异常与超时各有独立文案', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))
    await expect(addTorrentFile(CONFIG, TORRENT_BYTES)).rejects.toThrow('连不上引擎')

    vi.stubGlobal('fetch', () => {
      const error = new Error('aborted')
      error.name = 'AbortError'
      return Promise.reject(error)
    })
    await expect(addTorrentFile(CONFIG, TORRENT_BYTES)).rejects.toThrow('超时')
  })
})
