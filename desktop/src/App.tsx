import { useState } from 'react'
import { useSyncExternalStore } from 'react'
import { AddTaskDialog } from './components/AddTaskDialog'
import { TorrentList, type TorrentActions } from './components/TorrentList'
import type { EngineStore } from './store'
import { prepareTorrentFile, type TorrentBytes } from './add-task'
import { useDragDrop } from './use-drag-drop'

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

export interface AppProps {
  store: EngineStore
  /** 不传就没有添加入口：探针与测试渲染列表时不需要它 */
  onAddTask?: (magnet: string) => Promise<void>
  /** 不传则对话框只有磁力模式 */
  onAddFileTask?: (bytes: TorrentBytes, name: string) => Promise<void>
  /** 不传则行内不显示暂停/继续/删除按钮 */
  onRowActions?: TorrentActions
}

export function App({ store, onAddTask, onAddFileTask, onRowActions }: AppProps) {
  const snapshot = useSyncExternalStore(store.subscribeSnapshot, store.getSnapshot)
  const connection = useSyncExternalStore(
    store.subscribeConnection,
    store.getConnection,
  )
  const [adding, setAdding] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const torrents = snapshot?.torrents ?? []
  const totalDown = torrents.reduce((sum, t) => sum + t.rateDown, 0)
  const totalUp = torrents.reduce((sum, t) => sum + t.rateUp, 0)
  const canAdd = onAddTask !== undefined && connection.status !== 'offline'

  // 速率是瞬时量：引擎不在时这个数字必然已经不成立，继续显示就是在说谎。
  // 任务数则是「最后已知状态」，还有价值 —— 所以保留数字，但标出它不新鲜了。
  // 两者处理不同，理由不同，别合并成一个 online 开关。
  const offline = connection.status === 'offline'
  const rateText = (rate: number): string => (offline ? '—' : formatRate(rate))

  // 添加成功后不等弹窗自己关：先关掉（列表会在下一个 100ms 轮询里出现新任务），
  // 失败时 onSubmit 会把错误抛回对话框，那里才需要保持打开。
  const submit = async (magnet: string): Promise<void> => {
    if (onAddTask === undefined) return
    await onAddTask(magnet)
    setAdding(false)
    setActionError(null)
  }

  const submitFile = async (bytes: TorrentBytes, name: string): Promise<void> => {
    if (onAddFileTask === undefined) return
    await onAddFileTask(bytes, name)
    setAdding(false)
    setActionError(null)
  }

  const reportFailure = (failure: unknown): void => {
    setActionError(failure instanceof Error ? failure.message : String(failure))
  }

  // 拖入磁力：与对话框走同一个 submit，只是不需要经过输入框
  const dropMagnet = (magnet: string): void => {
    void submit(magnet).catch(reportFailure)
  }

  // 拖入种子文件：与「种子文件」页签走同一条 prepareTorrentFile → submitFile
  const dropFile = (file: File): void => {
    if (onAddFileTask === undefined) {
      setActionError('当前版本不支持文件添加')
      return
    }
    void (async () => {
      try {
        const prepared = await prepareTorrentFile(file)
        if (!prepared.ok) {
          setActionError(prepared.message)
          return
        }
        await submitFile(prepared.bytes, prepared.name)
      } catch (failure) {
        reportFailure(failure)
      }
    })()
  }

  // 只要接了添加入口就接管拖拽，**不**跟「引擎离线」联动：
  // 离线时拖进来立刻收到「连不上引擎」，比拖了毫无反应更接近事实。
  // 遮罩是唯一在 dragover 阶段能给出的反馈，关掉它等于让用户猜哪能放。
  const over = useDragDrop({
    enabled: onAddTask !== undefined,
    onMagnet: dropMagnet,
    onFile: dropFile,
    onReject: (message) => {
      setActionError(message)
    },
  })

  return (
    <div className="app">
      <header className="head">
        <h1>BT 下载器</h1>
        <div className="head-right">
          {onAddTask !== undefined && (
            <button
              type="button"
              className="btn-primary"
              data-testid="add-open"
              disabled={!canAdd}
              title={canAdd ? '粘贴磁力链接或选择种子文件' : '引擎离线，无法添加'}
              onClick={() => {
                setAdding(true)
              }}
            >
              + 添加任务
            </button>
          )}
          <div className="head-stats" data-testid="head-stats">
            <span>↓ {rateText(totalDown)}</span>
            <span>↑ {rateText(totalUp)}</span>
            <span>
              {snapshot?.count ?? 0} 个任务{offline ? '（离线）' : ''}
            </span>
          </div>
        </div>
      </header>

      {connection.status !== 'online' && (
        <div className={`banner banner-${connection.status}`}>
          {connection.status === 'connecting' ? '正在连接引擎…' : `引擎离线：${connection.detail ?? '未知原因'}`}
        </div>
      )}

      {actionError !== null && (
        <div className="banner banner-warn banner-dismissable" role="alert">
          <span>{actionError}</span>
          <button
            type="button"
            className="banner-close"
            data-testid="banner-dismiss"
            aria-label="关闭提示"
            onClick={() => {
              setActionError(null)
            }}
          >
            ✕
          </button>
        </div>
      )}

      {snapshot?.degraded && (
        <div className="banner banner-warn">部分字段无法解析，已按安全值降级显示</div>
      )}

      <TorrentList
        torrents={torrents}
        actions={onRowActions}
        stale={offline}
        onActionError={(message) => {
          setActionError(message)
        }}
      />

      <footer className="foot">
        <span>seq {snapshot?.seq ?? 0}</span>
        <span>{snapshot?.degraded ? '数据不完整' : '数据完整'}</span>
      </footer>

      {adding && onAddTask !== undefined && (
        <AddTaskDialog
          onCancel={() => {
            setAdding(false)
          }}
          onSubmit={submit}
          onSubmitFile={onAddFileTask === undefined ? undefined : submitFile}
        />
      )}

      {over && (
        <div className="drop-mask" data-testid="drop-mask">
          <div className="drop-hint">松手添加任务</div>
        </div>
      )}
    </div>
  )
}
