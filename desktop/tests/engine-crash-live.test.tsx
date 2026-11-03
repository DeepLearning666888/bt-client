/** @vitest-environment jsdom */

/** 真实引擎被强杀之后，界面会怎样。
 *
 * 与 engine-offline.test.tsx 的分工：
 *   - 那边验**显示规则**（哪些数字该消失、哪些该留下），数据源是可控的失效对象。
 *   - 这边验**真实链路**：真引擎、真 HTTP、真进程被杀。要证明的是「引擎消失时
 *     前端确实能察觉」，而不是卡在最后一份快照里假装一切正常。
 *
 * 为什么必须真杀进程：fetch 失败、连接被拒、进程还在但不响应，这三种失效在
 * 代码里走的分支不一样。stub 只能模拟第一种，而用户在任务管理器里点「结束任务」
 * 产生的正是「进程没了、端口没人接」这一种 —— 只有真杀才能证明它被正确处理。
 *
 * 由 desktop/tests/t_engine_crash.py 注入 ENGINE_URL / ENGINE_TOKEN / ENGINE_PID / EXPECT_IH。
 * 没有环境变量时跳过，直接 `vitest run` 不会假失败。
 */

import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { App } from '../src/App'
import { createEngineRuntime } from '../src/runtime'

const baseUrl = process.env['ENGINE_URL'] ?? ''
const token = process.env['ENGINE_TOKEN'] ?? ''
const expectIh = process.env['EXPECT_IH'] ?? ''
const pid = Number(process.env['ENGINE_PID'] ?? '0')
const hasEngine =
  baseUrl !== '' && token !== '' && expectIh !== '' && Number.isInteger(pid) && pid > 4

const runtime = hasEngine ? createEngineRuntime({ baseUrl, token }) : null

/** 在 act 里推进真实定时器，直到条件成立。
 *  不用 waitFor 的理由：这里的进度由 poller 的 setInterval 驱动，需要 React 在
 *  定时器回调里更新；手写循环能保证每一拍都包在 act 内，不产生未批处理的警告。 */
async function waitUntil(label: string, predicate: () => boolean, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100))
    })
  }
  throw new Error(`${timeoutMs}ms 内未达成「${label}」`)
}

afterEach(() => {
  cleanup()
})

it.skipIf(!hasEngine)(
  '强杀引擎：界面转离线 → 任务不丢 → 速率停写 → 不假装还在下载',
  async () => {
    expect(runtime).not.toBeNull()
    const stats = (): HTMLElement => screen.getByTestId('head-stats')

    render(
      <App
        store={runtime!.store}
        onAddTask={runtime!.addTask}
        onRowActions={runtime!.rowActions}
      />,
    )

    // 1) 先让界面拿到真实数据。没有这一步，后面「任务不丢」的断言没有意义 ——
    //    列表本来就是空的，怎么杀都不会变。
    await runtime!.addTask?.(`magnet:?xt=urn:btih:${expectIh}&dn=crash-test`)
    await waitUntil('任务出现在列表', () => screen.queryAllByTestId('torrent-row').length === 1)
    expect(within(stats()).getByText('1 个任务')).toBeTruthy()
    expect(screen.queryByText(/引擎离线/)).toBeNull()

    // 2) 强杀。TerminateProcess 级别 —— 不给引擎任何收尾机会，等同于任务管理器
    //    「结束任务」。SIGTERM 会让它走正常退出路径，测不出「进程凭空消失」。
    process.kill(pid, 'SIGKILL')

    // 3) 独立于界面先确认进程真的没了：否则下面 UI 断言失败时无法区分
    //    「界面没反应」和「进程根本没死」。轮询几轮而不是只试一次 ——
    //    TerminateProcess 之后端口释放有一小段延迟。
    let alive = true
    const deadDeadline = Date.now() + 5000
    while (Date.now() < deadDeadline) {
      try {
        await fetch(`${baseUrl}/health`, { headers: { Authorization: `Bearer ${token}` } })
      } catch {
        alive = false
        break
      }
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    expect(alive).toBe(false)

    // 4) 界面必须自己发现，不能等用户再点一下才发现
    await waitUntil('出现离线提示', () => screen.queryByText(/引擎离线/) !== null)
    const banner = screen.getByText(/引擎离线/)
    expect(banner.textContent).not.toContain('未知原因')

    // 5) 最后一份数据留着（用户加的种子不该凭空消失），但明确标成离线
    expect(screen.getAllByTestId('torrent-row')).toHaveLength(1)
    expect(within(stats()).getByText('1 个任务（离线）')).toBeTruthy()

    // 6) 瞬时量不再显示数字
    expect(within(stats()).getByText('↓ —')).toBeTruthy()
    expect(within(stats()).getByText('↑ —')).toBeTruthy()

    // 7) 行内按钮禁用：引擎不在了，点了必然失败，就不该让人点
    const removeBtn = screen.getByTestId('row-remove')
    expect((removeBtn as HTMLButtonElement).disabled).toBe(true)

    runtime!.dispose()
  },
  40_000,
)
