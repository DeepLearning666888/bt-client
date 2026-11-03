/** @vitest-environment jsdom */

/** AC-19：真实引擎上的暂停 / 恢复 / 删除。
 *
 * 由 desktop/tests/t_row_action_live.py 注入 ENGINE_URL / ENGINE_TOKEN / EXPECT_IH。
 * 没有环境变量时跳过，直接 `vitest run` 不会假失败。
 *
 * 三条都靠轮询 `/state` 判定，不信任 HTTP 返回的 `{"ok":true}`：
 * 引擎先改内部状态、快照由另一条路径生成，「请求成功」和「状态真的变了」
 * 是两件事 —— 只验前者的话，一个把动作记录丢掉却仍回 200 的实现也能过。
 */

import { expect, it } from 'vitest'
import { addTorrent } from '../src/add-task'
import { pauseTorrent, removeTorrent, resumeTorrent } from '../src/torrent-action'
import { parseSnapshot, type Snapshot, type Torrent } from '../src/api'

const baseUrl = process.env['ENGINE_URL'] ?? ''
const token = process.env['ENGINE_TOKEN'] ?? ''
const expectIh = process.env['EXPECT_IH'] ?? ''
const hasEngine = baseUrl !== '' && token !== '' && expectIh !== ''

const CONFIG = { baseUrl, token }
const MAGNET = `magnet:?xt=urn:btih:${expectIh}&dn=row-action`

async function fetchState(): Promise<unknown> {
  const response = await fetch(`${baseUrl}/state?since=-1`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok).toBe(true)
  return (await response.json()) as unknown
}

/** 轮询直到条件成立。超时不代表「慢」，代表状态没变过 —— 那才是要报的错。 */
async function waitUntil(
  label: string,
  predicate: (torrents: Torrent[]) => boolean,
  timeoutMs = 8000,
): Promise<Snapshot> {
  const deadline = Date.now() + timeoutMs
  let last: Snapshot | null = null
  while (Date.now() < deadline) {
    const snapshot = parseSnapshot(await fetchState())
    expect(snapshot).not.toBeNull()
    if (snapshot !== null) {
      last = snapshot
      if (predicate(snapshot.torrents)) return snapshot
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(
    `${timeoutMs}ms 内未达成「${label}」，最后状态：${JSON.stringify(
      last?.torrents.map((t) => ({ ih: t.infohash.slice(0, 8), state: t.state, paused: t.paused })),
    )}`,
  )
}

it.skipIf(!hasEngine)('暂停 → 状态变 paused → 继续 → 离开 paused → 删除 → 列表空', async () => {
  // 先造一个任务。用磁力而不是种子文件：这条验的是操作，不是上传。
  await addTorrent(CONFIG, MAGNET)
  await waitUntil('任务出现在列表', (list) => list.some((t) => t.infohash === expectIh))

  await pauseTorrent(CONFIG, expectIh)
  const paused = await waitUntil('任务被暂停', (list) => {
    const target = list.find((t) => t.infohash === expectIh)
    return target !== undefined && (target.paused || target.state === 'paused')
  })
  const pausedTarget = paused.torrents.find((t) => t.infohash === expectIh)
  expect(pausedTarget?.paused || pausedTarget?.state === 'paused').toBe(true)

  await resumeTorrent(CONFIG, expectIh)
  await waitUntil('任务恢复', (list) => {
    const target = list.find((t) => t.infohash === expectIh)
    return target !== undefined && !target.paused && target.state !== 'paused'
  })

  // 删除时不勾「同时删文件」：这个任务没有元数据、没有落盘文件，
  // 但走的是同一条代码路径，真正的差别在引擎侧是否删文件（由引擎单元测试覆盖）
  await removeTorrent(CONFIG, expectIh, false)
  await waitUntil('任务从列表消失', (list) => !list.some((t) => t.infohash === expectIh))

  // 再删一次必须是可读文案，而不是崩溃或静默成功
  await expect(removeTorrent(CONFIG, expectIh, false)).rejects.toThrow('已经不在了')
})
