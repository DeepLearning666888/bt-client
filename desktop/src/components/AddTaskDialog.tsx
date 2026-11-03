/** 添加任务对话框。
 *
 * 刻意做成「受控的哑组件」：它不知道自己发的请求去了哪里，只接收 onSubmit / onSubmitFile。
 * 这样父组件可以注入真实现（main.tsx 里接引擎）或假实现（测试里断言调用参数），
 * 而「什么算合法磁力」「文件多大算超限」都不在这一层。
 *
 * 两种模式共用一扇窗而不是两扇：用户的心智模型是「添加任务」这一件事，
 * 分成两个入口等于让他先选一个再点一次。
 */

import { useEffect, useRef, useState } from 'react'
import { normalizeMagnet, prepareTorrentFile, type TorrentBytes } from '../add-task'

type Mode = 'magnet' | 'file'

export interface AddTaskDialogProps {
  onCancel: () => void
  onSubmit: (magnet: string) => Promise<void>
  /** 不传就只有磁力模式：探针与不需要文件的场景少一份状态 */
  onSubmitFile?: (bytes: TorrentBytes, name: string) => Promise<void>
}

export function AddTaskDialog({ onCancel, onSubmit, onSubmitFile }: AddTaskDialogProps) {
  const [mode, setMode] = useState<Mode>('magnet')
  const [text, setText] = useState('')
  const [file, setFile] = useState<{ name: string; size: number; raw: File } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
    }
  }, [onCancel])

  const switchMode = (next: Mode): void => {
    if (pending || next === mode) return
    setMode(next)
    setError(null)
  }

  const submit = async (): Promise<void> => {
    if (pending) return

    if (mode === 'magnet') {
      const verdict = normalizeMagnet(text)
      if (!verdict.ok) {
        setError(verdict.message)
        return
      }
      setPending(true)
      setError(null)
      try {
        await onSubmit(verdict.magnet)
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : String(failure))
        setPending(false)
      }
      return
    }

    if (file === null) {
      setError('请先选择一个 .torrent 文件')
      return
    }
    if (onSubmitFile === undefined) {
      setError('当前版本不支持文件添加')
      return
    }

    setPending(true)
    setError(null)
    try {
      // 读字节 + 首字节校验都在 prepareTorrentFile 里，失败原因直接可显示
      const prepared = await prepareTorrentFile(file.raw)
      if (!prepared.ok) {
        setError(prepared.message)
        setPending(false)
        return
      }
      await onSubmitFile(prepared.bytes, prepared.name)
      // 成功后由父组件卸载本组件，这里不能再 setState（会警告已卸载组件）
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
      setPending(false)
    }
  }

  const sizeText = file === null ? '' : file.size >= 1024
    ? `${(file.size / 1024).toFixed(0)} KiB`
    : `${file.size} 字节`

  return (
    <div
      className="modal-mask"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="add-task-title">
        <h2 id="add-task-title">添加任务</h2>

        {onSubmitFile !== undefined && (
          <div className="modal-tabs" role="tablist" aria-label="添加方式">
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'magnet'}
              className={`modal-tab${mode === 'magnet' ? ' modal-tab-active' : ''}`}
              data-testid="tab-magnet"
              disabled={pending}
              onClick={() => {
                switchMode('magnet')
              }}
            >
              磁力链接
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'file'}
              className={`modal-tab${mode === 'file' ? ' modal-tab-active' : ''}`}
              data-testid="tab-file"
              disabled={pending}
              onClick={() => {
                switchMode('file')
              }}
            >
              种子文件
            </button>
          </div>
        )}

        {mode === 'magnet' ? (
          <>
            <p className="modal-hint">粘贴磁力链接，回车即可。支持 magnet:? 开头的链接。</p>
            <input
              // key 必须区分：两个模式都是 <input> 且在同一位置，React 会复用同一个
              // DOM 节点，value 在 string 与 undefined 之间跳会触发 controlled 警告
              key="magnet"
              ref={inputRef}
              className="modal-input"
              data-testid="magnet-input"
              value={text}
              placeholder="magnet:?xt=urn:btih:..."
              disabled={pending}
              aria-label="磁力链接"
              onChange={(event) => {
                setText(event.target.value)
                setError(null)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submit()
              }}
            />
          </>
        ) : (
          <>
            <p className="modal-hint">选一个 .torrent 文件，最大 8 MiB。</p>
            <input
              key="file"
              ref={fileRef}
              type="file"
              accept=".torrent,application/x-bittorrent"
              data-testid="file-input"
              className="modal-file-hidden"
              disabled={pending}
              onChange={(event) => {
                const picked = event.target.files?.[0] ?? null
                setFile(picked === null ? null : { name: picked.name, size: picked.size, raw: picked })
                setError(null)
              }}
            />
            <div className="modal-file-row">
              <button
                type="button"
                className="modal-btn"
                data-testid="file-pick"
                disabled={pending}
                onClick={() => {
                  fileRef.current?.click()
                }}
              >
                选择文件
              </button>
              <span className="modal-file-name" data-testid="file-name">
                {file === null ? '还没选文件' : `${file.name}（${sizeText}）`}
              </span>
            </div>
          </>
        )}

        {error !== null && (
          <div className="modal-error" data-testid="add-error" role="alert">
            {error}
          </div>
        )}
        <div className="modal-actions">
          <button type="button" className="modal-btn" onClick={onCancel} disabled={pending}>
            取消
          </button>
          <button
            type="button"
            className="modal-btn modal-btn-primary"
            data-testid="add-submit"
            onClick={() => void submit()}
            disabled={pending}
          >
            {pending ? '添加中…' : '添加'}
          </button>
        </div>
      </div>
    </div>
  )
}
