import { useSyncExternalStore } from 'react'
import { TorrentList } from './components/TorrentList'
import type { EngineStore } from './store'

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

export function App({ store }: { store: EngineStore }) {
  const snapshot = useSyncExternalStore(store.subscribeSnapshot, store.getSnapshot)
  const connection = useSyncExternalStore(
    store.subscribeConnection,
    store.getConnection,
  )

  const torrents = snapshot?.torrents ?? []
  const totalDown = torrents.reduce((sum, t) => sum + t.rateDown, 0)
  const totalUp = torrents.reduce((sum, t) => sum + t.rateUp, 0)

  return (
    <div className="app">
      <header className="head">
        <h1>BT 下载器</h1>
        <div className="head-stats">
          <span>↓ {formatRate(totalDown)}</span>
          <span>↑ {formatRate(totalUp)}</span>
          <span>{snapshot?.count ?? 0} 个任务</span>
        </div>
      </header>

      {connection.status !== 'online' && (
        <div className={`banner banner-${connection.status}`}>
          {connection.status === 'connecting' ? '正在连接引擎…' : `引擎离线：${connection.detail ?? '未知原因'}`}
        </div>
      )}

      {snapshot?.degraded && (
        <div className="banner banner-warn">部分字段无法解析，已按安全值降级显示</div>
      )}

      <TorrentList torrents={torrents} />

      <footer className="foot">
        <span>seq {snapshot?.seq ?? 0}</span>
        <span>{snapshot?.degraded ? '数据不完整' : '数据完整'}</span>
      </footer>
    </div>
  )
}
