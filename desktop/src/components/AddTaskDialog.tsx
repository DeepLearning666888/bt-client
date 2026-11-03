/** 添加任务对话框。
 *
 * 刻意做成「受控的哑组件」：它不知道自己发的请求去了哪里，只接收一个 onSubmit。
 * 这样父组件可以注入真实现（main.tsx 里接引擎）或假实现（测试里断言调用参数），
 * 而「什么算合法磁力」「失败怎么措辞」都不在这一层。
 */

import { useEffect, useRef, useState } from 'react'
import { normalizeMagnet } from '../add-task'

export interface AddTaskDialogProps {
  onCancel: () => void
  onSubmit: (magnet: string) => Promise<void>
}

export function AddTaskDialog({ onCancel, onSubmit }: AddTaskDialogProps) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

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

  const submit = async (): Promise<void> => {
    if (pending) return
    const verdict = normalizeMagnet(text)
    if (!verdict.ok) {
      setError(verdict.message)
      return
    }
    setPending(true)
    setError(null)
    try {
      await onSubmit(verdict.magnet)
      // 成功后由父组件卸载本组件，这里不能再 setState（会警告已卸载组件）
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
      setPending(false)
    }
  }

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
        <p className="modal-hint">粘贴磁力链接，回车即可。暂时只支持 magnet:? 开头的链接。</p>
        <input
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
