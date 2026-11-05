/** @vitest-environment jsdom */

/** AC-12：真实引擎端到端 —— 界面上的添加动作能否让任务真的出现在列表里。
 *
 * 由 desktop/tests/t_add_task_live.py 注入 ENGINE_URL / ENGINE_TOKEN / EXPECT_IH。
 * 没有环境变量时跳过，这样直接 `vitest run` 不会因为没起引擎假失败。
 *
 * 刻意走完整链路而不是只调一次 HTTP：addTorrent() 是界面真正调用的那个函数，
 * 它发出去的 URL/令牌/body 一旦和引擎对不上，这条就会红。
 */

import { expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { addTorrent } from '../src/add-task'
import { parseSnapshot, type Snapshot } from '../src/api'
import { EngineStore } from '../src/store'
import { App } from '../src/App'
import { STATE_LABELS } from '../src/labels'

const baseUrl = process.env['ENGINE_URL'] ?? ''
const token = process.env['ENGINE_TOKEN'] ?? ''
const expectIh = process.env['EXPECT_IH'] ?? ''
const hasEngine = baseUrl !== '' && token !== '' && expectIh !== ''

const MAGNET = `magnet:?xt=urn:btih:${expectIh}&dn=ui-add`

async function fetchState(): Promise<unknown> {
  const response = await fetch(`${baseUrl}/state?since=-1`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok).toBe(true)
  return (await response.json()) as unknown
}

async function waitForTask(infohash: string, timeoutMs = 8000): Promise<Snapshot> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const snapshot = parseSnapshot(await fetchState())
    expect(snapshot).not.toBeNull()
    if (snapshot !== null && snapshot.torrents.some((t) => t.infohash === infohash)) {
      return snapshot
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`8s 内 /state 里没有出现 ${infohash}`)
}

it.skipIf(!hasEngine)('前端添加 → 任务出现在列表且状态有中文文案', async () => {
  const config = { baseUrl, token }

  const added = await addTorrent(config, MAGNET)
  expect(added.infohash).toBe(expectIh)

  // 重复添加必须是可读文案，而不是「HTTP 409」
  await expect(addTorrent(config, MAGNET)).rejects.toThrow('已经在列表里')

  const snapshot = await waitForTask(expectIh)
  const target = snapshot.torrents.find((t) => t.infohash === expectIh)
  expect(target).toBeDefined()
  // 刚加进来还没拿到元数据，状态必须是引擎认识的那一批，不能落到 unknown
  expect(target?.state).not.toBe('unknown')

  const store = new EngineStore()
  store.applySnapshot(snapshot)
  render(<App store={store} onAddTask={() => Promise.resolve()} />)

  const rows = screen.getAllByTestId('torrent-row')
  expect(rows).toHaveLength(1)
  const row = rows[0]!
  // 名字可能来自 dn，也可能还没有元数据而用 infohash 兜底；两者都算对
  expect(row.textContent ?? '').toMatch(new RegExp(`ui-add|${expectIh.slice(0, 8)}`))
  expect(row.textContent ?? '').toContain(STATE_LABELS[target!.state])
  // 空列表的占位必须消失
  expect(screen.queryByText('还没有任务')).toBeNull()
  // C16：真实引擎的正常快照不能触发黄色降级横幅。
  // state-vocab 只证明 degraded=false；这里把 App 的条件渲染也一起锁住。
  expect(screen.queryByText(/部分字段无法解析/)).toBeNull()

  await waitFor(() => {
    expect(screen.getByText('1 个任务')).toBeTruthy()
  })
})
