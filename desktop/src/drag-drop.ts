/** 拖拽添加的判定层：只回答「拖进来的东西该走哪条路」，不碰 DOM 事件对象。
 *
 * 分成纯函数的目的和 add-task.ts 一样：`drop` 事件对象在 jsdom 里造起来很别扭，
 * 而真正容易写错的是**取舍规则**（多文件怎么办、什么情况该报错、什么情况该闭嘴），
 * 这部分完全可以脱离浏览器单测。
 *
 * 拖拽本身不新建通道 —— 判定完就交给对话框那两条路用的同一批函数，
 * 所以这里不需要重新论证「请求怎么发」。
 */

import { normalizeMagnet, validateTorrentFile, type FileLike } from './add-task'

/** 从 `DataTransfer` 里摘出来的、判定需要的那几样。 */
export interface DragPayload {
  /** `dataTransfer.types`，大小写不敏感 */
  types: readonly string[]
  files: readonly FileLike[]
  /** `dataTransfer.getData('text/plain')`；取不到时给空串 */
  text: string
}

export type DragPlan =
  | { kind: 'magnet'; magnet: string }
  | { kind: 'file'; file: FileLike }
  | { kind: 'reject'; message: string }
  /** 拖的不是我们能认的东西：不亮遮罩、不弹提示，当没发生 */
  | { kind: 'ignore' }

/** 遮罩判定。dragover 阶段浏览器**不允许**读出实际内容（安全限制），
 * 所以这里只能看类型。要按真实内容判断，只能等 drop —— 那时遮罩已经没意义了。 */
export function hasDraggableContent(types: readonly string[]): boolean {
  for (const type of types) {
    const lower = type.toLowerCase()
    // 'Files' 是标准写法；text/uri-list 是拖链接时浏览器给的类型
    if (lower === 'files' || lower === 'text/plain' || lower === 'text/uri-list') return true
  }
  return false
}

/** 拖入内容的路径判定。
 *
 * 优先级：文件 > 文本。拖文件时浏览器可能同时在 text/plain 里塞文件名，
 * 先看 files 才不会把 `ubuntu.torrent` 当成一段要校验的磁力文本。 */
export function planDrop(payload: DragPayload): DragPlan {
  const { files } = payload

  if (files.length === 1) {
    const verdict = validateTorrentFile(files[0])
    return verdict.ok
      ? { kind: 'file', file: files[0] }
      : { kind: 'reject', message: verdict.message }
  }

  if (files.length > 1) {
    // 不静默取第一个：一次加多个需要批量接口与部分失败处理，本轮不做；
    // 而只加第一个会让用户以为都加进去了 —— 那比直接说「不行」更坏。
    return {
      kind: 'reject',
      message: `一次只能拖一个种子文件（你拖了 ${files.length} 个）`,
    }
  }

  const text = payload.text.trim()
  if (text === '') return { kind: 'ignore' }

  const verdict = normalizeMagnet(text)
  if (verdict.ok) return { kind: 'magnet', magnet: verdict.magnet }

  // 只有「本来就打算当磁力链接」的文本才报错。拖拽是顺手动作，
  // 随手拖一段选中的文字进来就弹红字，等于把系统的困惑转嫁给用户。
  if (/^magnet:/i.test(text)) return { kind: 'reject', message: verdict.message }

  return { kind: 'ignore' }
}
