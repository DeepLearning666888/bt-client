/** @vitest-environment jsdom */

/** AC-13 的界面侧：文件模式的交互。
 *
 * 校验逻辑本身在 add-file.test.ts 里断言过，这里只验「界面怎么用那些结论」：
 * 没选文件不能提交、选了坏文件不发起请求、成功要关闭、失败要留窗。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { App } from '../src/App'
import { EngineStore } from '../src/store'
import type { Snapshot } from '../src/api'

const TORRENT_TEXT = 'd8:announce14:http://t/a/ann4:infod4:name4:demoee'

function makeStore(): EngineStore {
  const store = new EngineStore()
  const empty: Snapshot = { seq: 1, tsMs: 0, count: 0, torrents: [], degraded: false }
  store.applySnapshot(empty)
  return store
}

interface Handlers {
  onAddTask?: (magnet: string) => Promise<void>
  onAddFileTask?: (bytes: Uint8Array, name: string) => Promise<void>
}

function openDialog(handlers: Handlers): void {
  render(
    <App
      store={makeStore()}
      onAddTask={handlers.onAddTask ?? (() => Promise.resolve())}
      onAddFileTask={handlers.onAddFileTask}
    />,
  )
  fireEvent.click(screen.getByTestId('add-open'))
}

function pickFile(content: string, name: string): void {
  const file = new File([content], name, { type: 'application/x-bittorrent' })
  fireEvent.change(screen.getByTestId('file-input'), { target: { files: [file] } })
}

afterEach(() => {
  cleanup()
})

describe('模式切换', () => {
  it('没有文件处理器时不显示切换条', () => {
    openDialog({})
    expect(screen.queryByTestId('tab-file')).toBeNull()
    expect(screen.getByTestId('magnet-input')).toBeTruthy()
  })

  it('有文件处理器时两个 tab 都在，默认停在磁力', () => {
    openDialog({ onAddFileTask: () => Promise.resolve() })
    const magnetTab = screen.getByTestId('tab-magnet') as HTMLButtonElement
    const fileTab = screen.getByTestId('tab-file') as HTMLButtonElement
    expect(magnetTab.getAttribute('aria-selected')).toBe('true')
    expect(fileTab.getAttribute('aria-selected')).toBe('false')
    expect(screen.getByTestId('magnet-input')).toBeTruthy()
  })

  it('切到文件模式后输入框消失、选文件按钮出现', () => {
    openDialog({ onAddFileTask: () => Promise.resolve() })
    fireEvent.click(screen.getByTestId('tab-file'))
    expect(screen.queryByTestId('magnet-input')).toBeNull()
    expect(screen.getByTestId('file-pick')).toBeTruthy()
    expect(screen.getByTestId('file-name').textContent).toContain('还没选')
  })
})

describe('文件模式行为', () => {
  it('没选文件就提交 → 就地报错，不发起请求', async () => {
    const onAddFileTask = vi.fn(() => Promise.resolve())
    openDialog({ onAddFileTask })
    fireEvent.click(screen.getByTestId('tab-file'))
    fireEvent.click(screen.getByTestId('add-submit'))

    expect((await screen.findByTestId('add-error')).textContent).toContain('先选择')
    expect(onAddFileTask).not.toHaveBeenCalled()
  })

  it('选中文件后显示文件名与大小', async () => {
    openDialog({ onAddFileTask: () => Promise.resolve() })
    fireEvent.click(screen.getByTestId('tab-file'))
    pickFile(TORRENT_TEXT, 'demo.torrent')

    await waitFor(() => {
      const text = screen.getByTestId('file-name').textContent ?? ''
      expect(text).toContain('demo.torrent')
      expect(text).toMatch(/\d+ 字节/)
    })
  })

  it('合法种子提交成功 → 上层收到字节与文件名，对话框关闭', async () => {
    const received: Array<{ length: number; name: string; first: number }> = []
    const onAddFileTask = vi.fn((bytes: Uint8Array, name: string) => {
      received.push({ length: bytes.length, name, first: bytes[0] ?? -1 })
      return Promise.resolve()
    })
    openDialog({ onAddFileTask })
    fireEvent.click(screen.getByTestId('tab-file'))
    pickFile(TORRENT_TEXT, 'demo.torrent')
    fireEvent.click(screen.getByTestId('add-submit'))

    await waitFor(() => {
      expect(screen.queryByTestId('file-pick')).toBeNull()
    })
    expect(onAddFileTask).toHaveBeenCalledTimes(1)
    expect(received[0]?.name).toBe('demo.torrent')
    expect(received[0]?.first).toBe(0x64) // 'd'
    expect(received[0]?.length).toBe(TORRENT_TEXT.length)
  })

  it('后缀不是 .torrent → 拦下且不发请求', async () => {
    const onAddFileTask = vi.fn(() => Promise.resolve())
    openDialog({ onAddFileTask })
    fireEvent.click(screen.getByTestId('tab-file'))
    pickFile(TORRENT_TEXT, 'ubuntu.iso')
    fireEvent.click(screen.getByTestId('add-submit'))

    expect((await screen.findByTestId('add-error')).textContent).toContain('.torrent')
    expect(onAddFileTask).not.toHaveBeenCalled()
    // 对话框留着，让用户重选
    expect(screen.getByTestId('file-pick')).toBeTruthy()
  })

  it('改了后缀的假种子 → 提示「不像种子」', async () => {
    const onAddFileTask = vi.fn(() => Promise.resolve())
    openDialog({ onAddFileTask })
    fireEvent.click(screen.getByTestId('tab-file'))
    pickFile('这其实是一段中文文本', 'fake.torrent')
    fireEvent.click(screen.getByTestId('add-submit'))

    expect((await screen.findByTestId('add-error')).textContent).toContain('不像种子')
    expect(onAddFileTask).not.toHaveBeenCalled()
  })

  it('上传失败：显示原因、留窗、按钮恢复', async () => {
    const onAddFileTask = vi.fn(() => Promise.reject(new Error('这个任务已经在列表里了')))
    openDialog({ onAddFileTask })
    fireEvent.click(screen.getByTestId('tab-file'))
    pickFile(TORRENT_TEXT, 'demo.torrent')
    fireEvent.click(screen.getByTestId('add-submit'))

    expect((await screen.findByTestId('add-error')).textContent).toContain('已经在列表里')
    const button = screen.getByTestId('add-submit') as HTMLButtonElement
    await waitFor(() => {
      expect(button.disabled).toBe(false)
    })
  })

  it('切回磁力模式会清掉文件模式的报错', async () => {
    openDialog({ onAddFileTask: () => Promise.resolve() })
    fireEvent.click(screen.getByTestId('tab-file'))
    fireEvent.click(screen.getByTestId('add-submit'))
    await screen.findByTestId('add-error')

    fireEvent.click(screen.getByTestId('tab-magnet'))
    expect(screen.queryByTestId('add-error')).toBeNull()
    expect(screen.getByTestId('magnet-input')).toBeTruthy()
  })
})
