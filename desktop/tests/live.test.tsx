/** @vitest-environment jsdom */

/** 对接真实引擎：不 mock fetch，走本机回环。
 *
 * 由 desktop/tests/t_live_engine.py 注入 ENGINE_URL / ENGINE_TOKEN / EXPECT_NAME，
 * 它负责拉起引擎、放好本地种子、让任务进入 seeding，然后调用本测试。
 * 没有环境变量时跳过 —— 这样直接跑 vitest 不会因为没起引擎而假失败。
 */

import { expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { Poller } from '../src/poller'
import { TorrentList } from '../src/components/TorrentList'
import type { Snapshot } from '../src/api'

const baseUrl = process.env['ENGINE_URL']
const token = process.env['ENGINE_TOKEN']
const expectName = process.env['EXPECT_NAME'] ?? ''

const hasEngine = Boolean(baseUrl && token)

it.skipIf(!hasEngine)('真实引擎 → 解析层 → 渲染', async () => {
  const seen: Snapshot[] = []
  const errors: unknown[] = []
  const poller = new Poller(
    async (since) => {
      const response = await fetch(`${baseUrl}/state?since=${since}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!response.ok) throw new Error(`/state 返回 ${response.status}`)
      return (await response.json()) as unknown
    },
    (snapshot) => seen.push(snapshot),
    (error) => errors.push(error),
  )

  await poller.tick()
  expect(errors).toHaveLength(0)
  expect(seen).toHaveLength(1)

  const snapshot = seen[0]
  expect(snapshot).toBeDefined()
  if (snapshot === undefined) return

  expect(snapshot.torrents.length).toBeGreaterThan(0)
  expect(snapshot.degraded).toBe(false)

  const { container } = render(<TorrentList torrents={snapshot.torrents} />)
  expect(container.textContent).toContain(expectName)
  expect(container.querySelectorAll('[data-testid="torrent-row"]').length).toBeGreaterThan(0)

  // 真实引擎在 seq 未变时必须回 unchanged，这里顺带验证去重在真实数据上成立
  await poller.tick()
  expect(poller.stats.unchanged).toBe(1)
  expect(seen).toHaveLength(1)
})
