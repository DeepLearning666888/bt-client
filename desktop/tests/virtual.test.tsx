/** @vitest-environment jsdom */

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { TorrentList } from '../src/components/TorrentList'
import type { Torrent } from '../src/api'

function makeTorrents(count: number): Torrent[] {
  return Array.from({ length: count }, (_, index) => ({
    infohash: index.toString(16).padStart(40, '0'),
    name: `torrent-${index}`,
    state: 'downloading',
    paused: false,
    progress: 0.5,
    rateDown: 1024,
    rateUp: 128,
    peers: 4,
    seeds: 2,
    doneBytes: 512,
    totalBytes: 1024,
    etaS: 30,
    hasMetadata: true,
    error: null,
  }))
}

describe('TorrentList 虚拟滚动', () => {
  it('1000 个任务时只渲染视口内的行', () => {
    const { container } = render(<TorrentList torrents={makeTorrents(1000)} />)
    const rows = container.querySelectorAll('[data-testid="torrent-row"]')
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.length).toBeLessThanOrEqual(40)
  })

  it('渲染行数与任务总数脱钩：10 条与 1000 条数量级相当', () => {
    const small = render(<TorrentList torrents={makeTorrents(10)} />)
    const smallCount = small.container.querySelectorAll('[data-testid="torrent-row"]').length
    small.unmount()

    const large = render(<TorrentList torrents={makeTorrents(1000)} />)
    const largeCount = large.container.querySelectorAll('[data-testid="torrent-row"]').length

    // 大列表不会被渲染成全量，且上限被卡住
    expect(largeCount).toBeLessThanOrEqual(40)
    expect(largeCount).toBeLessThanOrEqual(smallCount + 12)
  })

  it('总高度按任务数撑开，滚动范围正确', () => {
    const { container } = render(<TorrentList torrents={makeTorrents(1000)} />)
    const spacer = container.querySelector('.list-spacer') as HTMLElement | null
    expect(spacer).not.toBeNull()
    expect(spacer?.style.height).toBe(`${1000 * 64}px`)
  })

  it('滚动到中段时窗口位移、行数仍受限', () => {
    const { container } = render(
      <TorrentList torrents={makeTorrents(1000)} scrollTop={32000} />,
    )
    const rows = container.querySelectorAll('[data-testid="torrent-row"]')
    expect(rows.length).toBeLessThanOrEqual(40)
    // 32000 / 64 = 500，减去 overscan 6 → 从第 494 条开始
    expect(rows[0]?.textContent).toContain('torrent-494')
  })

  it('空列表显示空态而不是空白', () => {
    const { container } = render(<TorrentList torrents={[]} />)
    expect(container.textContent).toContain('还没有任务')
    expect(container.querySelectorAll('[data-testid="torrent-row"]')).toHaveLength(0)
  })

  it('进度条用 scaleX 而不是 width', () => {
    const { container } = render(<TorrentList torrents={makeTorrents(1)} />)
    const fill = container.querySelector('.bar-fill') as HTMLElement | null
    expect(fill).not.toBeNull()
    expect(fill?.style.transform).toBe('scaleX(0.5)')
    expect(fill?.style.width).toBe('')
  })
})
