/** @vitest-environment jsdom */

/** AC-11：对话框交互。
 *
 * 这一层只验「界面行为」：非法输入不该发起请求、成功要关闭、失败要留窗并说明原因。
 * 磁力的合法性判定与请求形状已经在 add-task.test.ts 里断言过，这里不重复造轮子，
 * 所以 onSubmit 一律用 stub。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { App } from '../src/App'
import { EngineStore } from '../src/store'
import type { Snapshot } from '../src/api'

const IH = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
const MAGNET = `magnet:?xt=urn:btih:${IH}`

function makeStore(status: 'online' | 'offline' = 'online'): EngineStore {
  const store = new EngineStore()
  if (status === 'online') {
    const empty: Snapshot = { seq: 1, tsMs: 0, count: 0, torrents: [], degraded: false }
    store.applySnapshot(empty)
  } else {
    store.markOffline('测试用：引擎没起来')
  }
  return store
}

function openDialog(onSubmit: (magnet: string) => Promise<void>): void {
  render(<App store={makeStore()} onAddTask={onSubmit} />)
  fireEvent.click(screen.getByTestId('add-open'))
}

afterEach(() => {
  cleanup()
})

describe('添加入口', () => {
  it('没有注入 onAddTask 时不显示按钮', () => {
    render(<App store={makeStore()} />)
    expect(screen.queryByTestId('add-open')).toBeNull()
  })

  it('引擎离线时按钮禁用并说明原因', () => {
    render(<App store={makeStore('offline')} onAddTask={() => Promise.resolve()} />)
    const button = screen.getByTestId('add-open') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.title).toContain('离线')
  })

  it('点击后在焦点里给出输入框', async () => {
    openDialog(() => Promise.resolve())
    const input = screen.getByTestId('magnet-input') as HTMLInputElement
    await waitFor(() => {
      expect(document.activeElement).toBe(input)
    })
  })
})

describe('对话框行为', () => {
  it('非法磁力不发请求，就地报错', async () => {
    const onSubmit = vi.fn(() => Promise.resolve())
    openDialog(onSubmit)

    fireEvent.change(screen.getByTestId('magnet-input'), { target: { value: '不是磁力' } })
    fireEvent.click(screen.getByTestId('add-submit'))

    expect((await screen.findByTestId('add-error')).textContent).toContain('magnet:?')
    expect(onSubmit).not.toHaveBeenCalled()
    // 对话框必须留着，让用户改
    expect(screen.queryByTestId('magnet-input')).not.toBeNull()
  })

  it('合法磁力回车提交，成功后对话框关闭', async () => {
    const onSubmit = vi.fn(() => Promise.resolve())
    openDialog(onSubmit)

    const input = screen.getByTestId('magnet-input')
    fireEvent.change(input, { target: { value: `  ${MAGNET}  ` } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      expect(screen.queryByTestId('magnet-input')).toBeNull()
    })
    expect(onSubmit).toHaveBeenCalledTimes(1)
    // 去掉空白后再交给上层，避免把首尾空格当内容发给引擎
    expect(onSubmit).toHaveBeenCalledWith(MAGNET)
  })

  it('提交失败：显示错误、保持打开、按钮恢复可点', async () => {
    const onSubmit = vi.fn(() => Promise.reject(new Error('这个任务已经在列表里了')))
    openDialog(onSubmit)

    fireEvent.change(screen.getByTestId('magnet-input'), { target: { value: MAGNET } })
    fireEvent.click(screen.getByTestId('add-submit'))

    expect((await screen.findByTestId('add-error')).textContent).toContain('已经在列表里')
    expect(screen.queryByTestId('magnet-input')).not.toBeNull()
    const button = screen.getByTestId('add-submit') as HTMLButtonElement
    await waitFor(() => {
      expect(button.disabled).toBe(false)
    })
  })

  it('提交中按钮文案变化且禁止重复点击', async () => {
    // 用对象装 resolve：直接写 let release 会让 TS 在调用点把它窄化成 null
    const gate: { release: (() => void) | null } = { release: null }
    const onSubmit = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          gate.release = resolve
        }),
    )
    openDialog(onSubmit)

    fireEvent.change(screen.getByTestId('magnet-input'), { target: { value: MAGNET } })
    const button = screen.getByTestId('add-submit') as HTMLButtonElement
    fireEvent.click(button)

    await waitFor(() => {
      expect(button.textContent).toContain('添加中')
    })
    fireEvent.click(button)
    expect(onSubmit).toHaveBeenCalledTimes(1)

    gate.release?.()
    await waitFor(() => {
      expect(screen.queryByTestId('magnet-input')).toBeNull()
    })
  })

  it('Esc 关闭对话框', async () => {
    const onSubmit = vi.fn(() => Promise.resolve())
    openDialog(onSubmit)
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByTestId('magnet-input')).toBeNull()
    })
    expect(onSubmit).not.toHaveBeenCalled()
  })
})
