/** 状态词表一致性：前端认不认得引擎吐出来的每一个状态。
 *
 * 这组用例存在的原因是一次真实事故：磁力任务加进去后引擎返回 `state:"metadata"`，
 * 前端词表里没有 `metadata`，于是解析成 `unknown`，并把整份快照标成 degraded ——
 * 界面上表现为「任务名正常、状态显示 unknown、顶部一条『部分字段无法解析』」。
 * 单测当时全绿，因为用例里的状态是自己编的 `downloading`。
 *
 * 所以这里不编数据：**直接从引擎源码里抓字面量**。引擎加一个状态、前端没跟上，
 * 这条测试就红。反过来前端多认几个引擎暂时不发的（queued / allocating）不算错。
 */

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { KNOWN_STATES, parseSnapshot, parseTorrent } from '../src/api'
import { STATE_LABELS } from '../src/labels'

const HERE = dirname(fileURLToPath(import.meta.url))
const ENGINE_CPP = resolve(HERE, '../../engine/src/engine.cpp')

/** 引擎会吐的所有状态字面量，从源码里抓，不手抄。 */
function engineStates(): string[] {
  const src = readFileSync(ENGINE_CPP, 'utf8')

  const fnAt = src.indexOf('char const* state_name(')
  if (fnAt < 0) throw new Error('engine.cpp 里找不到 state_name()，抓取规则失效了')
  const body = src.slice(fnAt, src.indexOf('\n}', fnAt))
  const fromSwitch = [...body.matchAll(/return\s+"([a-z_]+)"/g)].map((m) => m[1])

  // rebuild_state() 在拿到 handle 之前把 state 初始化成 pending，不经过 state_name()
  const initAt = src.indexOf('std::string state = "pending"')
  if (initAt < 0) throw new Error('engine.cpp 里找不到 pending 的初始化，抓取规则失效了')
  const fromInit = [...src.slice(initAt, initAt + 40).matchAll(/"([a-z_]+)"/g)].map((m) => m[1])

  return Array.from(new Set([...fromSwitch, ...fromInit]))
}

describe('引擎 → 前端 状态词表', () => {
  it('能从引擎源码里抓到状态（抓不到说明匹配规则已失效，测试本身不可信）', () => {
    const states = engineStates()
    expect(states.length).toBeGreaterThanOrEqual(5)
    expect(states).toContain('metadata')
    expect(states).toContain('pending')
  })

  it('引擎吐出的每个状态，前端都认识', () => {
    const missing = engineStates().filter((s) => !(KNOWN_STATES as readonly string[]).includes(s))
    expect(missing).toEqual([])
  })

  it('前端认识的每个状态都有中文文案', () => {
    const blank = KNOWN_STATES.filter((s) => {
      const label = STATE_LABELS[s]
      return typeof label !== 'string' || label.trim().length === 0
    })
    expect(blank).toEqual([])
  })

  it('状态文案里不残留英文标识符', () => {
    for (const state of KNOWN_STATES) {
      expect(STATE_LABELS[state]).not.toMatch(/^[a-z_]+$/)
    }
  })
})

describe('真实引擎快照不触发降级', () => {
  const base = {
    infohash: 'dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c',
    name: 'Big Buck Bunny',
    paused: false,
    has_metadata: false,
    progress: 0,
    total_bytes: 0,
    done_bytes: 0,
    rate_down: 0,
    rate_up: 0,
    peers: 0,
    seeds: 0,
    eta_s: -1,
    save_path: '',
    error: null,
    added_ms: 1793363617928,
  }

  it('磁力任务刚添加（state=metadata）不降级', () => {
    const parsed = parseTorrent({ ...base, state: 'metadata' })
    expect(parsed?.torrent.state).toBe('metadata')
    expect(parsed?.degraded).toBe(false)
  })

  it('下载完成（state=finished）不降级', () => {
    const parsed = parseTorrent({ ...base, state: 'finished', has_metadata: true, progress: 1 })
    expect(parsed?.torrent.state).toBe('finished')
    expect(parsed?.degraded).toBe(false)
  })

  it('引擎真会吐的其它状态都不降级', () => {
    for (const state of ['checking', 'downloading', 'seeding', 'paused', 'other']) {
      const parsed = parseTorrent({ ...base, state })
      expect(parsed?.degraded, `state=${state} 不该被判降级`).toBe(false)
    }
  })

  it('整份快照：一条磁力任务 → 不降级', () => {
    const snapshot = parseSnapshot({
      seq: 12,
      ts_ms: 1793363618941,
      torrent_count: 1,
      torrents: [{ ...base, state: 'metadata' }],
    })
    expect(snapshot?.degraded).toBe(false)
    expect(snapshot?.count).toBe(1)
  })

  it('真的不认识的词仍然是 unknown + 降级（降级机制不能被改死）', () => {
    const parsed = parseTorrent({ ...base, state: 'weird_future_state' })
    expect(parsed?.torrent.state).toBe('unknown')
    expect(parsed?.degraded).toBe(true)
  })
})
