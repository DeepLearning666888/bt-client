/** @vitest-environment jsdom */

/** AC-22：窗口级拖拽交互。
 *
 * 判定规则已经在 drag-drop.test.ts 里钉住了，这里只验「接线」：
 * 遮罩什么时候亮什么时候灭、drop 有没有被分发到对的那个回调、
 * 以及那条最容易漏的 —— `dragover` 到底有没有被 preventDefault。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { App } from '../src/App'
import { EngineStore } from '../src/store'
import type { Snapshot } from '../src/api'

const MAGNET =
  'magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny'

const TORRENT_TEXT = 'd8:announce14:http://t/a/ann4:infod4:name4:demoee'

function makeStore(): EngineStore {
  const store = new EngineStore()
  const empty: Snapshot = { seq: 1, tsMs: 0, count: 0, torrents: [], degraded: false }
  store.applySnapshot(empty)
  return store
}

interface FakeDrag {
  types?: string[]
  files?: File[]
  text?: string
  /** 模拟「拖出窗口」时浏览器给 null */
  relatedTarget?: EventTarget | null
}

/** jsdom 没有 DataTransfer，直接往事件上挂一个够用的替身。
 * `getData` 只在 drop 阶段被调用 —— 也只有那时浏览器才允许读出内容。 */
function dragEvent(type: string, data: FakeDrag = {}): Event {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'dataTransfer', {
    value: {
      types: data.types ?? [],
      files: data.files ?? [],
      dropEffect: 'none',
      getData: (format: string): string => (format === 'text/plain' ? (data.text ?? '') : ''),
    },
  })
  if (data.relatedTarget !== undefined) {
    Object.defineProperty(event, 'relatedTarget', { value: data.relatedTarget })
  }
  return event
}

async function fire(event: Event): Promise<boolean> {
  let notCancelled = true
  await act(async () => {
    notCancelled = window.dispatchEvent(event)
  })
  return notCancelled
}

interface Handlers {
  onAddTask?: ReturnType<typeof vi.fn>
  onAddFileTask?: ReturnType<typeof vi.fn>
}

function mount(handlers: Handlers = {}): void {
  render(
    <App
      store={makeStore()}
      {...(handlers.onAddTask === undefined
        ? {}
        : { onAddTask: handlers.onAddTask as unknown as (m: string) => Promise<void> })}
      {...(handlers.onAddFileTask === undefined
        ? {}
        : {
            onAddFileTask: handlers.onAddFileTask as unknown as (
              b: Uint8Array,
              n: string,
            ) => Promise<void>,
          })}
    />,
  )
}

function maskShown(): boolean {
  return screen.queryByTestId('drop-mask') !== null
}

afterEach(() => {
  cleanup()
})

describe('遮罩开关', () => {
  it('拖入文件时亮遮罩', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()) })
    expect(maskShown()).toBe(false)
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    expect(maskShown()).toBe(true)
  })

  it('拖着离开时遮罩消失', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()) })
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    await fire(dragEvent('dragleave', { types: ['Files'] }))
    expect(maskShown()).toBe(false)
  })

  it('在子元素之间移动（enter-leave 成对但计数未归零）时遮罩不闪', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()) })
    // 进入父元素，再进入子元素 —— 浏览器会再发一次 dragenter
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    // 离开父元素进子元素，一次 dragleave
    await fire(dragEvent('dragleave', { types: ['Files'], relatedTarget: document.body }))
    expect(maskShown()).toBe(true)
    // 计数归零才灭
    await fire(dragEvent('dragleave', { types: ['Files'], relatedTarget: document.body }))
    expect(maskShown()).toBe(false)
  })

  it('relatedTarget 为 null 表示真的离开窗口，不等计数立即撤', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()) })
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    await fire(dragEvent('dragleave', { types: ['Files'], relatedTarget: null }))
    expect(maskShown()).toBe(false)
  })

  it('没接添加入口时完全不接管拖拽', async () => {
    mount({})
    await fire(dragEvent('dragenter', { types: ['Files'] }))
    expect(maskShown()).toBe(false)
  })

  it('拖应用私有类型不亮遮罩', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()) })
    await fire(dragEvent('dragenter', { types: ['application/x-photoshop-layer'] }))
    expect(maskShown()).toBe(false)
  })
})

describe('dragover 必须阻止默认行为', () => {
  it('否则浏览器会把文件当导航目标直接打开，页面当场被换掉', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()) })
    const notCancelled = await fire(dragEvent('dragover', { types: ['Files'] }))
    expect(notCancelled).toBe(false)
  })
})

describe('drop 分发', () => {
  it('拖入磁力文本 → 走添加磁力', async () => {
    const onAddTask = vi.fn(() => Promise.resolve())
    mount({ onAddTask })
    await fire(dragEvent('drop', { types: ['text/plain'], text: MAGNET }))
    await waitFor(() => {
      expect(onAddTask).toHaveBeenCalledWith(MAGNET)
    })
    expect(maskShown()).toBe(false)
  })

  it('拖入 .torrent 文件 → 走文件添加，名字对得上', async () => {
    // 显式写参数类型：vi.fn(() => ...) 的参数元组是空元组，
    // 访问 calls[0][1] 会被 TS 判成「越界」
    const onAddFileTask = vi.fn((_bytes: Uint8Array, _name: string) => Promise.resolve())
    mount({ onAddTask: vi.fn(() => Promise.resolve()), onAddFileTask })
    const file = new File([TORRENT_TEXT], 'demo.torrent', { type: 'application/x-bittorrent' })
    await fire(dragEvent('drop', { types: ['Files'], files: [file], text: 'demo.torrent' }))
    await waitFor(() => {
      expect(onAddFileTask).toHaveBeenCalledTimes(1)
    })
    // 第二个参数是文件名：证明传过去的是真文件而不是判定层里的 name/size 影子对象
    expect(onAddFileTask.mock.calls[0][1]).toBe('demo.torrent')
  })

  it('drop 也阻止默认行为，否则文件仍会被浏览器打开', async () => {
    mount({ onAddTask: vi.fn(() => Promise.resolve()), onAddFileTask: vi.fn(() => Promise.resolve()) })
    const file = new File([TORRENT_TEXT], 'demo.torrent', { type: 'application/x-bittorrent' })
    const notCancelled = await fire(
      dragEvent('drop', { types: ['Files'], files: [file], text: 'demo.torrent' }),
    )
    expect(notCancelled).toBe(false)
  })

  it('拖入非种子文件 → 显示原因，且一次请求都不发', async () => {
    const onAddTask = vi.fn(() => Promise.resolve())
    const onAddFileTask = vi.fn(() => Promise.resolve())
    mount({ onAddTask, onAddFileTask })
    const file = new File(['x'], 'ubuntu.iso', { type: 'application/octet-stream' })
    await fire(dragEvent('drop', { types: ['Files'], files: [file] }))
    const banner = screen.getByRole('alert')
    expect(banner.textContent).toContain('ubuntu.iso')
    expect(onAddTask).not.toHaveBeenCalled()
    expect(onAddFileTask).not.toHaveBeenCalled()
  })

  it('拖入多个文件 → 提示只支持一个，不静默取第一个', async () => {
    const onAddFileTask = vi.fn(() => Promise.resolve())
    mount({ onAddTask: vi.fn(() => Promise.resolve()), onAddFileTask })
    const a = new File([TORRENT_TEXT], 'a.torrent', { type: 'application/x-bittorrent' })
    const b = new File([TORRENT_TEXT], 'b.torrent', { type: 'application/x-bittorrent' })
    await fire(dragEvent('drop', { types: ['Files'], files: [a, b] }))
    expect(screen.getByRole('alert').textContent).toContain('2 个')
    expect(onAddFileTask).not.toHaveBeenCalled()
  })

  it('拖入无关文本 → 什么都不发生，也不弹提示', async () => {
    const onAddTask = vi.fn(() => Promise.resolve())
    mount({ onAddTask })
    await fire(dragEvent('drop', { types: ['text/plain'], text: '随便一段文字' }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onAddTask).not.toHaveBeenCalled()
    expect(maskShown()).toBe(false)
  })

  it('引擎出错时把原因显示出来，而不是静默吞掉', async () => {
    const onAddTask = vi.fn(() => Promise.reject(new Error('连不上引擎，本地引擎可能没在运行')))
    mount({ onAddTask })
    await fire(dragEvent('drop', { types: ['text/plain'], text: MAGNET }))
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toContain('连不上引擎')
    })
  })

  it('没接文件处理却拖了文件 → 明确说不支持，不发半截请求', async () => {
    const onAddTask = vi.fn(() => Promise.resolve())
    mount({ onAddTask })
    const file = new File([TORRENT_TEXT], 'demo.torrent', { type: 'application/x-bittorrent' })
    await fire(dragEvent('drop', { types: ['Files'], files: [file], text: 'demo.torrent' }))
    expect(screen.getByRole('alert').textContent).toContain('不支持文件添加')
    expect(onAddTask).not.toHaveBeenCalled()
  })
})
