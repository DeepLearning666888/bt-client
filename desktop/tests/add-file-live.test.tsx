/** @vitest-environment jsdom */

/** AC-15：真实引擎端到端 —— 选一个真种子文件，任务能否出现在列表里。
 *
 * 由 desktop/tests/t_add_file_live.py 注入 ENGINE_URL / ENGINE_TOKEN / TORRENT_PATH。
 * 没有环境变量时跳过，这样直接 `vitest run` 不会因为没起引擎假失败。
 *
 * 断言 infohash 与文件名里的前缀一致（那个 fixture 的文件名自带 infohash 前 8 位），
 * 这是「字节在传输中没被改坏」的直接证据 —— 前端要是把字节当 UTF-8 文本处理过，
 * 哈希一定变。
 */

import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { addTorrentFile, type TorrentBytes } from '../src/add-task'
import { parseSnapshot, type Snapshot } from '../src/api'
import { EngineStore } from '../src/store'
import { App } from '../src/App'
import { STATE_LABELS } from '../src/labels'

const baseUrl = process.env['ENGINE_URL'] ?? ''
const token = process.env['ENGINE_TOKEN'] ?? ''
const torrentPath = process.env['TORRENT_PATH'] ?? ''
const expectPrefix = process.env['EXPECT_PREFIX'] ?? ''
const hasEngine = baseUrl !== '' && token !== '' && torrentPath !== ''

function readTorrent(): TorrentBytes {
  const raw = readFileSync(torrentPath)
  // 复制到独立的 ArrayBuffer：Buffer 池化后的 byteOffset 不一定为 0
  return new Uint8Array(
    raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength) as ArrayBuffer,
  )
}

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

it.skipIf(!hasEngine)('种子文件上传 → infohash 正确 → 任务出现在列表', async () => {
  const config = { baseUrl, token }
  const bytes = readTorrent()
  expect(bytes.length).toBeGreaterThan(0)

  const added = await addTorrentFile(config, bytes)
  expect(added.infohash).toMatch(/^[0-9a-f]{40}$/)
  // 文件名自带的前 8 位必须在算出来的哈希里 —— 字节没被改坏的硬证据
  expect(added.infohash.startsWith(expectPrefix)).toBe(true)

  // 重复上传同一个种子：必须是可读文案，而不是 HTTP 409
  await expect(addTorrentFile(config, bytes)).rejects.toThrow('已经在列表里')

  const snapshot = await waitForTask(added.infohash)
  const target = snapshot.torrents.find((t) => t.infohash === added.infohash)
  expect(target).toBeDefined()
  expect(target?.state).not.toBe('unknown')

  const store = new EngineStore()
  store.applySnapshot(snapshot)
  render(<App store={store} onAddTask={() => Promise.resolve()} />)

  const rows = screen.getAllByTestId('torrent-row')
  expect(rows.length).toBeGreaterThanOrEqual(1)
  const row = rows.find((r) => (r.textContent ?? '').includes(STATE_LABELS[target!.state]))
  expect(row).toBeDefined()
  expect(screen.queryByText('还没有任务')).toBeNull()

  await waitFor(() => {
    expect(screen.getByText('1 个任务')).toBeTruthy()
  })
})
