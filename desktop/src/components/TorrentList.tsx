/** 任务列表：固定行高虚拟滚动。
 *
 * 目标不是「支持十万条」，而是让 DOM 节点数与任务总数脱钩 —— 1000 条和 10 条
 * 渲染出来的行数一样。固定行高换掉「逐行测量高度」的复杂度，代价是不能有
 * 高度不定的行（当前设计里没有）。
 *
 * 位移用 transform 而不是 top：top 触发逐帧重排，transform 走合成层。
 *
 * 行内动作（暂停/继续/删除）只在这一层出现，因为「哪一行」只有这里知道。
 * 具体请求由外部注入的 `actions` 发出 —— 组件不知道 HTTP 长什么样。
 */

import { useState } from 'react'
import type { UIEvent } from 'react'
import type { Torrent } from '../api'
import { STATE_LABELS } from '../labels'
import { rowActionFor } from '../torrent-action'
import { ConfirmRemoveDialog } from './ConfirmRemoveDialog'

export const DEFAULT_ROW_HEIGHT = 64
export const DEFAULT_VIEWPORT_HEIGHT = 480
export const DEFAULT_OVERSCAN = 6

export interface TorrentActions {
  pause: (infohash: string) => Promise<void>
  resume: (infohash: string) => Promise<void>
  /** 返回 null = 干净完成；返回字符串 = 任务删了但有没做成的事（如文件被占用） */
  remove: (infohash: string, deleteFiles: boolean) => Promise<string | null>
}

export interface TorrentListProps {
  torrents: Torrent[]
  rowHeight?: number
  viewportHeight?: number
  overscan?: number
  /** 受控滚动位置，便于测试；不传则组件自己管 */
  scrollTop?: number
  onScrollTopChange?: (value: number) => void
  /** 不传就没有行内按钮：探针与只读场景不需要它 */
  actions?: TorrentActions
  /** 动作失败时往上抛一句人话，由上层决定显示在哪 */
  onActionError?: (message: string) => void
  /** 动作成功但有没做成的事（当前唯一来源：文件被占用，没删掉） */
  onActionWarning?: (message: string) => void
  /** 数据已停止更新（引擎离线）。速率属于瞬时量，此时不再显示数字；
   * 动作按钮禁用 —— 点了必然失败，那就别让人点。 */
  stale?: boolean
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

interface RowProps {
  torrent: Torrent
  height: number
  /** 没注入 actions 时不渲染按钮：探针渲染 1000 行时多两个按钮会污染性能基线 */
  withActions: boolean
  busy: boolean
  stale: boolean
  onToggle: (torrent: Torrent, action: 'pause' | 'resume') => void
  onAskRemove: (torrent: Torrent) => void
}

function TorrentRow({
  torrent,
  height,
  withActions,
  busy,
  stale,
  onToggle,
  onAskRemove,
}: RowProps) {
  const action = rowActionFor(torrent.state, torrent.paused)
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
        <span className="row-rate">↓ {stale ? '—' : formatRate(torrent.rateDown)}</span>
        <span className="row-rate">↑ {stale ? '—' : formatRate(torrent.rateUp)}</span>
        {withActions && action !== null && (
          <button
            type="button"
            className="row-btn"
            data-testid={`row-${action}`}
            disabled={busy || stale}
            title={stale ? '引擎离线，暂时不能操作' : action === 'pause' ? '暂停这个任务' : '继续下载'}
            onClick={(event) => {
              event.stopPropagation()
              onToggle(torrent, action)
            }}
          >
            {action === 'pause' ? '暂停' : '继续'}
          </button>
        )}
        {withActions && (
          <button
            type="button"
            className="row-btn row-btn-danger"
            data-testid="row-remove"
            disabled={busy || stale}
            title="从列表移除"
            onClick={(event) => {
              event.stopPropagation()
              onAskRemove(torrent)
            }}
          >
            删除
          </button>
        )}
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
  actions,
  onActionError,
  onActionWarning,
  stale = false,
}: TorrentListProps) {
  const [innerScrollTop, setInnerScrollTop] = useState(0)
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set())
  const [confirming, setConfirming] = useState<Torrent | null>(null)
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

  /** 把单个任务标成「进行中」。同一任务连点两下时第二次直接忽略 ——
   * 双击删除会把「确认」变成两次请求。
   *
   * 任务返回的字符串是「部分完成」的说明（任务删了但文件没删掉），走警告通道；
   * 抛异常才是失败。两者不能混：显示成失败会让用户以为任务还在。 */
  const run = async (
    infohash: string,
    task: () => Promise<string | null | void>,
  ): Promise<void> => {
    if (busy.has(infohash)) return
    setBusy((prev) => new Set(prev).add(infohash))
    try {
      const warning = await task()
      if (typeof warning === 'string' && warning !== '') onActionWarning?.(warning)
    } catch (failure) {
      onActionError?.(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setBusy((prev) => {
        const next = new Set(prev)
        next.delete(infohash)
        return next
      })
    }
  }

  const toggle = (torrent: Torrent, action: 'pause' | 'resume'): void => {
    if (actions === undefined) return
    void run(torrent.infohash, () =>
      action === 'pause' ? actions.pause(torrent.infohash) : actions.resume(torrent.infohash),
    )
  }

  const confirmRemove = (deleteFiles: boolean): void => {
    if (actions === undefined || confirming === null) return
    const target = confirming
    void run(target.infohash, async () => {
      const warning = await actions.remove(target.infohash, deleteFiles)
      // 有 warning 也算成功（任务确实删了），所以要关对话框；
      // 只有抛异常才留着 —— 那种情况任务可能还在，关掉会让人以为删掉了。
      setConfirming(null)
      return warning
    })
  }

  if (total === 0) {
    return (
      <div className="list list-empty" style={{ height: viewportHeight }}>
        还没有任务
      </div>
    )
  }

  return (
    <>
      <div
        className="list"
        data-stale={stale ? '1' : '0'}
        style={{ height: viewportHeight }}
        onScroll={handleScroll}
      >
        <div className="list-spacer" style={{ height: total * rowHeight }}>
          <div
            className="list-window"
            style={{ transform: `translateY(${first * rowHeight}px)` }}
          >
            {window.map((torrent) => (
              <TorrentRow
                key={torrent.infohash}
                torrent={torrent}
                height={rowHeight}
                withActions={actions !== undefined}
                busy={busy.has(torrent.infohash)}
                stale={stale}
                onToggle={toggle}
                onAskRemove={(target) => {
                  setConfirming(target)
                }}
              />
            ))}
          </div>
        </div>
      </div>

      {confirming !== null && actions !== undefined && (
        <ConfirmRemoveDialog
          name={confirming.name}
          pending={busy.has(confirming.infohash)}
          onCancel={() => {
            setConfirming(null)
          }}
          onConfirm={confirmRemove}
        />
      )}
    </>
  )
}
