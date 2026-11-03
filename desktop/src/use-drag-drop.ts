/** 窗口级拖拽接驳：只负责「遮罩开关」和「把 drop 内容交给判定」，不做业务。
 *
 * 为什么挂在 window 上而不是列表容器上：拖文件最容易的落点是窗口任意空白处。
 * 只接列表的话，用户拖到标题栏就什么都不会发生 —— 那种「拖了没反应」比报错更让人困惑。
 *
 * 两个必须踩对的坑：
 *   1. `dragover` 一定要 `preventDefault()`。不阻止默认行为，`drop` 根本不会触发，
 *      浏览器会拿拖入的文件当导航目标直接打开它，整个页面当场被替换掉。
 *   2. `dragenter` / `dragleave` 进出子元素时成对触发，所以遮罩不能用布尔量，
 *      要用进出计数 —— 否则鼠标划过列表里任意一行，遮罩就闪一下没了。
 */

import { useEffect, useRef, useState } from 'react'
import { hasDraggableContent, planDrop } from './drag-drop'

export interface DragDropOptions {
  /** 关掉时完全不接管拖拽：没接添加入口（探针、只读渲染）时不该抢事件 */
  enabled: boolean
  onMagnet: (magnet: string) => void
  onFile: (file: File) => void
  /** 拖进来了，但内容不能用；文案由判定层给，这里只负责显示 */
  onReject: (message: string) => void
}

/** drop 之外浏览器处于 protected mode，`getData` 一律返回空串；
 * 但仍然包一层 try —— 个别环境在特定时刻会直接抛。 */
function safeGetData(data: DataTransfer, format: string): string {
  try {
    return data.getData(format)
  } catch {
    return ''
  }
}

export function useDragDrop(options: DragDropOptions): boolean {
  const [over, setOver] = useState(false)
  const depth = useRef(0)

  // 用 ref 存最新回调：把 options 放进 effect 依赖会让每次渲染都重绑监听，
  // 而重绑会把 depth 计数丢掉 —— 遮罩正好卡在「绑了新监听、旧计数没带过来」的缝里。
  const latest = useRef(options)
  latest.current = options

  useEffect(() => {
    const usable = (event: DragEvent): boolean => {
      const data = event.dataTransfer
      return latest.current.enabled && data !== null && hasDraggableContent(data.types)
    }

    const clear = (): void => {
      depth.current = 0
      setOver(false)
    }

    const onEnter = (event: DragEvent): void => {
      if (!usable(event)) return
      event.preventDefault()
      depth.current += 1
      setOver(true)
    }

    const onOver = (event: DragEvent): void => {
      if (!usable(event)) return
      event.preventDefault()
      if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'copy'
    }

    const onLeave = (event: DragEvent): void => {
      if (!latest.current.enabled) return
      depth.current = Math.max(0, depth.current - 1)
      // relatedTarget 为 null 表示真的离开了窗口，不必等计数归零 ——
      // 计数在「拖出窗口」这条路径上不一定会配对触发，遮罩会卡住不消失。
      if (depth.current === 0 || event.relatedTarget === null) clear()
    }

    const onDrop = (event: DragEvent): void => {
      if (!latest.current.enabled) return
      const data = event.dataTransfer
      // 无论后面认不认，都要先撤遮罩并阻止浏览器打开文件
      clear()
      if (data === null) return
      event.preventDefault()

      const plan = planDrop({
        types: [...data.types],
        files: [...data.files].map((file) => ({ name: file.name, size: file.size })),
        text: safeGetData(data, 'text/plain'),
      })

      if (plan.kind === 'magnet') {
        latest.current.onMagnet(plan.magnet)
      } else if (plan.kind === 'file') {
        // 判定层只拿到 name/size，真文件要从 dataTransfer 里取原件读字节。
        // planDrop 保证这条分支时 files 恰好 1 个，所以取 [0] 是安全的。
        const raw = data.files[0]
        if (raw !== undefined) latest.current.onFile(raw)
      } else if (plan.kind === 'reject') {
        latest.current.onReject(plan.message)
      }
    }

    window.addEventListener('dragenter', onEnter)
    window.addEventListener('dragover', onOver)
    window.addEventListener('dragleave', onLeave)
    window.addEventListener('drop', onDrop)
    // 兜底：拖拽源消失（比如从别的程序拖过来后松手在窗口外）时不一定有 dragleave
    window.addEventListener('blur', clear)
    window.addEventListener('dragend', clear)

    return () => {
      window.removeEventListener('dragenter', onEnter)
      window.removeEventListener('dragover', onOver)
      window.removeEventListener('dragleave', onLeave)
      window.removeEventListener('drop', onDrop)
      window.removeEventListener('blur', clear)
      window.removeEventListener('dragend', clear)
    }
  }, [])

  return over
}
