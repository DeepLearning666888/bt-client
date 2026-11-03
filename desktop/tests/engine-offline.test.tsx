/** @vitest-environment jsdom */

/** 引擎中途失效后的界面表现。
 *
 * 判据来源：specs/desktop.md「引擎崩溃时 UI 的表现未验证」。这里补的是**显示规则**——
 * 数据源失效后界面上留下什么、不再显示什么。
 *
 * 为什么用可失效的 source 而不是杀进程：需要精确控制「最后一次成功是哪个快照」和
 * 「从哪一拍开始失败」。真杀进程只能证明链路会走到离线分支（由 engine-crash-live
 * 覆盖），断言的时点永远在跟操作系统赛跑。
 *
 * 最要防的形态：引擎已经不在了，界面还写着「↓ 1.2 MiB/s」——瞬时量一旦停更，
 * 继续显示就是在说谎，用户会以为还在下。
 */

import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '../src/App'
import { createEngineRuntime } from '../src/runtime'

const TORRENT = {
  infohash: 'b'.repeat(40),
  name: 'debian-12.iso',
  state: 'downloading',
  paused: false,
  progress: 0.42,
  rate_down: 1_310_720,
  rate_up: 4096,
  peers: 12,
  seeds: 5,
  done_bytes: 42_000_000,
  total_bytes: 100_000_000,
  eta_s: 44,
  has_metadata: true,
  error: '',
}

const SNAPSHOT = {
  seq: 11,
  ts_ms: 1_700_000_000_000,
  torrent_count: 1,
  torrents: [TORRENT],
}

interface Harness {
  runtime: ReturnType<typeof createEngineRuntime>
  break: () => void
  heal: () => void
}

function mountLive(): Harness {
  let broken = false
  const runtime = createEngineRuntime(
    { baseUrl: 'http://127.0.0.1:1', token: 't' },
    {
      source: async () => {
        if (broken) throw new Error('fetch failed')
        return SNAPSHOT
      },
    },
  )
  render(<App store={runtime.store} onAddTask={async () => {}} />)
  return {
    runtime,
    break: () => {
      broken = true
    },
    heal: () => {
      broken = false
    },
  }
}

afterEach(() => {
  cleanup()
})

describe('引擎失效后的界面', () => {
  it('离线提示写明原因，不是「未知原因」', async () => {
    const harness = mountLive()
    await waitFor(() => {
      expect(screen.queryByText(/引擎离线/)).toBeNull()
    })

    await act(async () => {
      harness.break()
      await harness.runtime.poller?.tick()
    })

    const banner = await screen.findByText(/引擎离线/)
    // detail 来自底层异常。给的是英文栈错误也无所谓，但「未知原因」说明
    // 失败路径没把 detail 带上来，那是代码问题不是数据问题。
    expect(banner.textContent).not.toContain('未知原因')
    expect(banner.textContent).toContain('fetch failed')
    harness.runtime.dispose()
  })

  it('已有的任务不会凭空消失，也不变成 0 个', async () => {
    const harness = mountLive()
    await waitFor(() => {
      expect(screen.getByText('1 个任务')).toBeTruthy()
    })

    await act(async () => {
      harness.break()
      await harness.runtime.poller?.tick()
    })

    // 数据消失比数据陈旧更糟：用户会以为自己加的种子没了。
    // 保留旧列表 + 明说离线，才是诚实的形态。
    expect(screen.queryByText('0 个任务')).toBeNull()
    expect(screen.getByText(/1 个任务/)).toBeTruthy()
    expect(screen.getByText('debian-12.iso')).toBeTruthy()
    harness.runtime.dispose()
  })

  it('停止更新的速率不再显示数字', async () => {
    const harness = mountLive()
    const stats = (): HTMLElement => screen.getByTestId('head-stats')
    await waitFor(() => {
      expect(within(stats()).getByText('↓ 1.3 MiB/s')).toBeTruthy()
    })
    // 在线时行内也显示速率：这是下面「离线后到处都没有数字」的对照基线
    expect(screen.getAllByText('↓ 1.3 MiB/s')).toHaveLength(2)

    await act(async () => {
      harness.break()
      await harness.runtime.poller?.tick()
    })

    // 下载速率是瞬时量：引擎不在了，这个数字必然已经不成立。
    // 汇总与行内一起查 —— 只改 header 而留着行内那两列，等于半个修复。
    expect(screen.queryByText('↓ 1.3 MiB/s')).toBeNull()
    expect(screen.queryByText('↑ 4.0 KiB/s')).toBeNull()
    expect(within(stats()).getByText('↓ —')).toBeTruthy()
    expect(within(stats()).getByText('↑ —')).toBeTruthy()
    harness.runtime.dispose()
  })

  it('离线时任务数仍可读，但明确标出已停止更新', async () => {
    const harness = mountLive()
    await waitFor(() => {
      expect(screen.getByText('1 个任务')).toBeTruthy()
    })

    await act(async () => {
      harness.break()
      await harness.runtime.poller?.tick()
    })

    // 「1 个任务」单独出现会被理解成「1 个正在跑」。加后缀不改数字，
    // 但把「这是最后已知状态」说清楚。
    expect(screen.getByText('1 个任务（离线）')).toBeTruthy()
    harness.runtime.dispose()
  })

  it('引擎回来后自动恢复，不需要用户重开窗口', async () => {
    const harness = mountLive()
    await waitFor(() => {
      expect(screen.getByText('1 个任务')).toBeTruthy()
    })

    await act(async () => {
      harness.break()
      await harness.runtime.poller?.tick()
    })
    expect(screen.getByText(/引擎离线/)).toBeTruthy()

    // 关键在「同一个运行时」能自己回来。离线若是单向门，用户只能重启应用 ——
    // 而引擎被系统更新、杀软、任务管理器干掉都是现实里会发生的事。
    await act(async () => {
      harness.heal()
      await harness.runtime.poller?.tick()
    })

    await waitFor(() => {
      expect(screen.queryByText(/引擎离线/)).toBeNull()
    })
    expect(screen.getByText('1 个任务')).toBeTruthy()
    expect(within(screen.getByTestId('head-stats')).getByText('↓ 1.3 MiB/s')).toBeTruthy()
    harness.runtime.dispose()
  })
})
