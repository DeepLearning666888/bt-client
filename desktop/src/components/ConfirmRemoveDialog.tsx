/** 删除确认。
 *
 * 为什么删除要弹窗而暂停不要：暂停能点回来，删除不能。
 *
 * 「要不要连文件一起删」没有默认正确值 —— 默认删文件会丢数据，默认不删文件会让
 * 用户以为磁盘干净了其实没有。所以把选择摆出来，默认停在更保守的「只删任务」。
 */

import { useEffect, useRef, useState } from 'react'

export interface ConfirmRemoveDialogProps {
  name: string
  pending: boolean
  onCancel: () => void
  onConfirm: (deleteFiles: boolean) => void
}

export function ConfirmRemoveDialog({
  name,
  pending,
  onCancel,
  onConfirm,
}: ConfirmRemoveDialogProps) {
  const [deleteFiles, setDeleteFiles] = useState(false)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    // 焦点落在「取消」而不是「删除」：回车不该是危险动作
    cancelRef.current?.focus()
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && !pending) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
    }
  }, [onCancel, pending])

  return (
    <div
      className="modal-mask"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !pending) onCancel()
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="remove-title">
        <h2 id="remove-title">删除任务</h2>
        <p className="modal-hint" data-testid="remove-name">
          {name}
        </p>
        <label className="modal-check">
          <input
            type="checkbox"
            data-testid="remove-files"
            checked={deleteFiles}
            disabled={pending}
            onChange={(event) => {
              setDeleteFiles(event.target.checked)
            }}
          />
          <span>同时删除已经下载的文件</span>
        </label>
        <p className="modal-hint">
          {deleteFiles ? '文件会从磁盘上删掉，不能恢复。' : '只从列表里移除，已下载的文件留在磁盘上。'}
        </p>
        <div className="modal-actions">
          <button
            ref={cancelRef}
            type="button"
            className="modal-btn"
            data-testid="remove-cancel"
            disabled={pending}
            onClick={onCancel}
          >
            取消
          </button>
          <button
            type="button"
            className="modal-btn modal-btn-danger"
            data-testid="remove-confirm"
            disabled={pending}
            onClick={() => {
              onConfirm(deleteFiles)
            }}
          >
            {pending ? '删除中…' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
