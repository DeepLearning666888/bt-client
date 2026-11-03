/** @vitest-environment jsdom */

/** AC-18：行内按钮的交互。
 *
 * 请求形状已经在 row-action.test.ts 里断言过，这里只验界面行为：
 * 点了是不是真调、删除是不是一定要确认、进行中会不会重复发。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TorrentList, type TorrentActions } from '../src/components/TorrentList'
import type { Torrent, TorrentState } from '../src/api'

const IH = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'

function makeTorrent(over: Partial<Torrent> = {}): Torrent {
  return {
    infohash: IH,
    name: '演示任务',
    state: 'downloading' as TorrentState,
    paused: false,
    progress: 0.25,
    rateDown: 1024,
    rateUp: 0,
    peers: 3,
    seeds: 1,
    doneBytes: 1024,
    totalBytes: 4096,
    etaS: 30,
    hasMetadata: true,
    error: null,
    ...over,
  }
}

function makeActions(over: Partial<TorrentActions> = {}): TorrentActions {
  return {
    pause: vi.fn(() => Promise.resolve()),
    resume: vi.fn(() => Promise.resolve()),
    remove: vi.fn(() => Promise.resolve()),
    ...over,
  }
}

afterEach(() => {
  cleanup()
})

describe('行内按钮显示', () => {
  it('没有注入 actions 时不显示任何按钮', () => {
    render(<TorrentList torrents={[makeTorrent()]} />)
    expect(screen.queryByTestId('row-pause')).toBeNull()
    expect(screen.queryByTestId('row-remove')).toBeNull()
  })

  it('下载中的行显示「暂停」与「删除」', () => {
    render(<TorrentList torrents={[makeTorrent()]} actions={makeActions()} />)
    expect(screen.getByTestId('row-pause').textContent).toBe('暂停')
    expect(screen.queryByTestId('row-resume')).toBeNull()
    expect(screen.getByTestId('row-remove')).toBeTruthy()
  })

  it('已暂停的行显示「继续」', () => {
    render(
      <TorrentList
        torrents={[makeTorrent({ state: 'paused', paused: true })]}
        actions={makeActions()}
      />,
    )
    expect(screen.getByTestId('row-resume').textContent).toBe('继续')
    expect(screen.queryByTestId('row-pause')).toBeNull()
  })

  it('已完成的行只有删除，没有暂停/继续', () => {
    render(
      <TorrentList
        torrents={[makeTorrent({ state: 'finished', paused: false, progress: 1 })]}
        actions={makeActions()}
      />,
    )
    expect(screen.queryByTestId('row-pause')).toBeNull()
    expect(screen.queryByTestId('row-resume')).toBeNull()
    expect(screen.getByTestId('row-remove')).toBeTruthy()
  })
})

describe('暂停与恢复', () => {
  it('点暂停立刻生效，不弹确认', async () => {
    const actions = makeActions()
    render(<TorrentList torrents={[makeTorrent()]} actions={actions} />)

    fireEvent.click(screen.getByTestId('row-pause'))

    await waitFor(() => {
      expect(actions.pause).toHaveBeenCalledWith(IH)
    })
    // 暂停是可逆动作，不该有确认框
    expect(screen.queryByTestId('remove-confirm')).toBeNull()
  })

  it('点继续调 resume', async () => {
    const actions = makeActions()
    render(
      <TorrentList
        torrents={[makeTorrent({ state: 'paused', paused: true })]}
        actions={actions}
      />,
    )
    fireEvent.click(screen.getByTestId('row-resume'))
    await waitFor(() => {
      expect(actions.resume).toHaveBeenCalledWith(IH)
    })
  })

  it('请求未完成时按钮禁用，连点两次只发一次', async () => {
    const gate: { release: (() => void) | null } = { release: null }
    const pause = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          gate.release = resolve
        }),
    )
    render(<TorrentList torrents={[makeTorrent()]} actions={makeActions({ pause })} />)

    const button = screen.getByTestId('row-pause') as HTMLButtonElement
    fireEvent.click(button)
    await waitFor(() => {
      expect(button.disabled).toBe(true)
    })
    fireEvent.click(button)
    expect(pause).toHaveBeenCalledTimes(1)

    gate.release?.()
    await waitFor(() => {
      expect(button.disabled).toBe(false)
    })
  })
})

describe('删除确认', () => {
  it('点删除先弹确认，未确认前不发请求', () => {
    const actions = makeActions()
    render(<TorrentList torrents={[makeTorrent()]} actions={actions} />)

    fireEvent.click(screen.getByTestId('row-remove'))

    expect(screen.getByTestId('remove-confirm')).toBeTruthy()
    expect(screen.getByTestId('remove-name').textContent).toContain('演示任务')
    expect(actions.remove).not.toHaveBeenCalled()
  })

  it('直接确认时默认只删任务、不删文件', async () => {
    const actions = makeActions()
    render(<TorrentList torrents={[makeTorrent()]} actions={actions} />)
    fireEvent.click(screen.getByTestId('row-remove'))
    fireEvent.click(screen.getByTestId('remove-confirm'))

    await waitFor(() => {
      expect(actions.remove).toHaveBeenCalledWith(IH, false)
    })
    await waitFor(() => {
      expect(screen.queryByTestId('remove-confirm')).toBeNull()
    })
  })

  it('勾选「同时删除文件」后传 true', async () => {
    const actions = makeActions()
    render(<TorrentList torrents={[makeTorrent()]} actions={actions} />)
    fireEvent.click(screen.getByTestId('row-remove'))
    fireEvent.click(screen.getByTestId('remove-files'))
    fireEvent.click(screen.getByTestId('remove-confirm'))

    await waitFor(() => {
      expect(actions.remove).toHaveBeenCalledWith(IH, true)
    })
  })

  it('取消不发请求，弹窗关闭', () => {
    const actions = makeActions()
    render(<TorrentList torrents={[makeTorrent()]} actions={actions} />)
    fireEvent.click(screen.getByTestId('row-remove'))
    fireEvent.click(screen.getByTestId('remove-cancel'))

    expect(screen.queryByTestId('remove-confirm')).toBeNull()
    expect(actions.remove).not.toHaveBeenCalled()
  })

  it('Esc 关闭确认框', () => {
    render(<TorrentList torrents={[makeTorrent()]} actions={makeActions()} />)
    fireEvent.click(screen.getByTestId('row-remove'))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('remove-confirm')).toBeNull()
  })
})

describe('失败处理', () => {
  it('暂停失败：把原因交给上层，行还在', async () => {
    const onActionError = vi.fn()
    const pause = vi.fn(() => Promise.reject(new Error('引擎找不到这个任务，它可能刚被删除')))
    render(
      <TorrentList
        torrents={[makeTorrent()]}
        actions={makeActions({ pause })}
        onActionError={onActionError}
      />,
    )

    fireEvent.click(screen.getByTestId('row-pause'))
    await waitFor(() => {
      expect(onActionError).toHaveBeenCalledWith('引擎找不到这个任务，它可能刚被删除')
    })
    expect(screen.getAllByTestId('torrent-row')).toHaveLength(1)
  })

  it('删除失败：确认框留着，让用户看到失败原因后重试', async () => {
    const onActionError = vi.fn()
    const remove = vi.fn(() => Promise.reject(new Error('引擎处理失败：磁盘忙')))
    render(
      <TorrentList
        torrents={[makeTorrent()]}
        actions={makeActions({ remove })}
        onActionError={onActionError}
      />,
    )

    fireEvent.click(screen.getByTestId('row-remove'))
    fireEvent.click(screen.getByTestId('remove-confirm'))

    await waitFor(() => {
      expect(onActionError).toHaveBeenCalledWith('引擎处理失败：磁盘忙')
    })
    expect(screen.getByTestId('remove-confirm')).toBeTruthy()
  })
})
