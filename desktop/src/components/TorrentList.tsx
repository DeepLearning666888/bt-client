/** 任务列表：固定行高虚拟滚动。
 *
 * 目标不是「支持十万条」，而是让 DOM 节点数与任务总数脱钩 —— 1000 条和 10 条
 * 渲染出来的行数一样。固定行高换掉「逐行测量高度」的复杂度，代价是不能有
 * 高度不定的行（当前设计里没有）。
 *
 * 位移用 transform 而不是 top：top 触发逐帧重排，transform 走合成层。
 */

import { useState } from 'react'
import type { UIEvent } from 'react'
import type { Torrent } from '../api'
import { STATE_LABELS } from '../labels'

export const DEFAULT_ROW_HEIGHT = 64
export const DEFAULT_VIEWPORT_HEIGHT = 480
export const DEFAULT_OVERSCAN = 6

export interface TorrentListProps {
  torrents: Torrent[]
  rowHeight?: number
  viewportHeight?: number
  overscan?: number
  /** 受控滚动位置，便于测试；不传则组件自己管 */
  scrollTop?: number
  onScrollTopChange?: (value: number) => void
}

function formatRate(bytesPerSecond: number): string {
  if (bytesPerSecond <= 0) return '—'
  const units = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s']
  let value = bytesPerSecond
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

function formatProgress(progress: number): string {
  return `${(progress * 100).toFixed(1)}%`
}

function TorrentRow({ torrent, height }: { torrent: Torrent; height: number }) {
  return (
    <div className="row" data-testid="torrent-row" style={{ height }}>
      <div className="row-main">
        <span className="row-name" title={torrent.name}>
          {torrent.name}
        </span>
        <span className="row-meta">
          {STATE_LABELS[torrent.state]} · {torrent.peers} peer · {torrent.seeds} seed ·{' '}
          {formatProgress(torrent.progress)}
        </span>
      </div>
      <div className="row-side">
        <span className="row-rate">↓ {formatRate(torrent.rateDown)}</span>
        <span className="row-rate">↑ {formatRate(torrent.rateUp)}</span>
      </div>
      <div className="bar-track">
        <div className="bar-fill" style={{ transform: `scaleX(${torrent.progress})` }} />
      </div>
    </div>
  )
}

export function TorrentList({
  torrents,
  rowHeight = DEFAULT_ROW_HEIGHT,
  viewportHeight = DEFAULT_VIEWPORT_HEIGHT,
  overscan = DEFAULT_OVERSCAN,
  scrollTop: controlledScrollTop,
  onScrollTopChange,
}: TorrentListProps) {
  const [innerScrollTop, setInnerScrollTop] = useState(0)
  const scrollTop = controlledScrollTop ?? innerScrollTop

  const total = torrents.length
  const capacity = Math.ceil(viewportHeight / rowHeight) + overscan * 2
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan)
  const last = Math.min(total, first + capacity)
  const window = torrents.slice(first, last)

  const handleScroll = (event: UIEvent<HTMLDivElement>) => {
    const value = event.currentTarget.scrollTop
    if (onScrollTopChange) onScrollTopChange(value)
    else setInnerScrollTop(value)
  }

  if (total === 0) {
    return (
      <div className="list list-empty" style={{ height: viewportHeight }}>
        还没有任务
      </div>
    )
  }

  return (
    <div className="list" style={{ height: viewportHeight }} onScroll={handleScroll}>
      <div className="list-spacer" style={{ height: total * rowHeight }}>
        <div
          className="list-window"
          style={{ transform: `translateY(${first * rowHeight}px)` }}
        >
          {window.map((torrent) => (
            <TorrentRow key={torrent.infohash} torrent={torrent} height={rowHeight} />
          ))}
        </div>
      </div>
    </div>
  )
}
