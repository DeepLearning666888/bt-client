import { describe, expect, it } from 'vitest'
import { parseSnapshot, parseTorrent } from '../src/api'

const validTorrent = {
  infohash: 'a'.repeat(40),
  name: 'debian.iso',
  state: 'downloading',
  paused: false,
  progress: 0.5,
  rate_down: 1024,
  rate_up: 256,
  peers: 3,
  seeds: 2,
  done_bytes: 1024,
  total_bytes: 2048,
  eta_s: 60,
  has_metadata: true,
  error: '',
}

describe('parseSnapshot 整体结构', () => {
  it('空 torrents 列表 → 0 条且不降级', () => {
    const snapshot = parseSnapshot({ seq: 7, ts_ms: 1, torrent_count: 0, torrents: [] })
    expect(snapshot).not.toBeNull()
    expect(snapshot?.torrents).toHaveLength(0)
    expect(snapshot?.seq).toBe(7)
    expect(snapshot?.degraded).toBe(false)
  })

  it('非对象 → null', () => {
    for (const raw of [null, undefined, 42, 'x', []]) {
      expect(parseSnapshot(raw)).toBeNull()
    }
  })

  it('torrents 不是数组 → null（结构不可用，按离线处理）', () => {
    expect(parseSnapshot({ seq: 1, torrents: 'nope' })).toBeNull()
    expect(parseSnapshot({ seq: 1 })).toBeNull()
  })

  it('缺 seq → seq 归零并标记降级', () => {
    const snapshot = parseSnapshot({ torrents: [validTorrent] })
    expect(snapshot?.seq).toBe(0)
    expect(snapshot?.degraded).toBe(true)
  })

  it('torrent_count 与实际条数不符 → 标记降级', () => {
    const snapshot = parseSnapshot({ seq: 1, torrent_count: 5, torrents: [validTorrent] })
    expect(snapshot?.count).toBe(1)
    expect(snapshot?.degraded).toBe(true)
  })
})

describe('parseTorrent 字段降级', () => {
  it('完整字段被原样保留', () => {
    const parsed = parseTorrent(validTorrent)
    expect(parsed).not.toBeNull()
    expect(parsed?.degraded).toBe(false)
    expect(parsed?.torrent.rateDown).toBe(1024)
    expect(parsed?.torrent.progress).toBe(0.5)
    expect(parsed?.torrent.etaS).toBe(60)
    expect(parsed?.torrent.error).toBeNull()
  })

  it('字段类型错误 → 该字段降级、其余字段保留', () => {
    const parsed = parseTorrent({ ...validTorrent, rate_down: '123', peers: null })
    expect(parsed).not.toBeNull()
    expect(parsed?.torrent.rateDown).toBe(0)
    expect(parsed?.torrent.peers).toBe(0)
    // 没坏的字段不能被牵连
    expect(parsed?.torrent.rateUp).toBe(256)
    expect(parsed?.torrent.name).toBe('debian.iso')
    expect(parsed?.degraded).toBe(true)
  })

  it('无元数据的磁力 → 用 infohash 前缀兜底命名', () => {
    const parsed = parseTorrent({
      infohash: 'b'.repeat(40),
      state: 'downloading',
      has_metadata: false,
      progress: 0,
    })
    expect(parsed?.torrent.name).toBe(`磁力 ${'b'.repeat(8)}`)
    expect(parsed?.torrent.hasMetadata).toBe(false)
  })

  it('缺 infohash → 丢弃（没有身份就没法做列表 key）', () => {
    expect(parseTorrent({ name: 'x', state: 'downloading' })).toBeNull()
    expect(parseTorrent({ infohash: '', name: 'x' })).toBeNull()
  })

  it('progress 越界被夹到 0..1', () => {
    expect(parseTorrent({ ...validTorrent, progress: 1.7 })?.torrent.progress).toBe(1)
    expect(parseTorrent({ ...validTorrent, progress: -3 })?.torrent.progress).toBe(0)
    expect(parseTorrent({ ...validTorrent, progress: Number.NaN })?.torrent.progress).toBe(0)
  })

  it('未知 state → unknown，且不因为不认识就丢掉任务', () => {
    const parsed = parseTorrent({ ...validTorrent, state: 'weird-state' })
    expect(parsed?.torrent.state).toBe('unknown')
    expect(parsed?.torrent.name).toBe('debian.iso')
  })

  it('eta 为负数或缺失 → null', () => {
    expect(parseTorrent({ ...validTorrent, eta_s: -1 })?.torrent.etaS).toBeNull()
    expect(parseTorrent({ ...validTorrent, eta_s: null })?.torrent.etaS).toBeNull()
  })

  it('坏任务被跳过、好任务保留，整体标记降级', () => {
    const snapshot = parseSnapshot({
      seq: 3,
      torrent_count: 2,
      torrents: [validTorrent, { name: '没有 infohash' }],
    })
    expect(snapshot?.torrents).toHaveLength(1)
    expect(snapshot?.degraded).toBe(true)
  })
})
