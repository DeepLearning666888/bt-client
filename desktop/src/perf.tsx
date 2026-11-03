/** 渲染与帧率探针的页面侧编排。
 *
 * 分工：统计口径与结论分档全在 perf-harness.ts（可单测），这里只负责
 * 「真的把组件挂起来、真的滚、真的每 100ms 换一次数据、真的数帧」。
 *
 * 三条不自欺的原则：
 *   1. 渲染的是 TorrentList 本体，不是仿写的假列表
 *   2. 滚动由 rAF 逐帧推进 scrollTop，不是 scrollTo 一次跳到底
 *   3. 更新走 setState，与真实 poller 的路径一致；不在 rAF 回调里 flushSync
 *      伪造成本（那样测出来的数字会比真实好）
 */

import { useLayoutEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { TorrentList } from './components/TorrentList'
import {
  SCENARIOS,
  buildReport,
  collectCssText,
  detectAnimationViolations,
  estimateRefresh,
  isSoftwareRenderer,
  makeTorrents,
  summarizeFrames,
  thresholdsFor,
} from './perf-harness'
import type {
  DisplayInfo,
  EnvFingerprint,
  PerfReport,
  SceneResult,
  ScenarioDef,
  Thresholds,
} from './perf-harness'
import type { Torrent } from './api'
import './styles.css'

const STAGE_HEIGHT = 480
/** 滚动速度：2000px/s 接近用户快速拖滚动条的手感，且 3s 内不会走完 1000 行 */
const SCROLL_PX_PER_SECOND = 2000
const COOLDOWN_MS = 500
/** 刷新率校准窗口。600ms 在 48Hz 下约 29 个间隔，足够取中位数又不至于让人干等 */
const CALIBRATE_MS = 600

// ---------------------------------------------------------------- DOM 小工具

function must(id: string): HTMLElement {
  const el = document.getElementById(id)
  if (el === null) throw new Error(`缺少 #${id}`)
  return el
}

function makeLogger(pre: HTMLElement): (line: string) => void {
  return (line) => {
    pre.textContent = pre.textContent === '' ? line : `${pre.textContent}\n${line}`
    pre.scrollTop = pre.scrollHeight
  }
}

function waitFrames(count: number): Promise<void> {
  return new Promise((resolve) => {
    let left = count
    const tick = (): void => {
      left -= 1
      if (left <= 0) resolve()
      else requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
}

function waitMs(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}

// ---------------------------------------------------------------- 环境指纹

function probeRenderer(): string {
  const canvas = document.createElement('canvas')
  try {
    const gl = canvas.getContext('webgl')
    if (gl === null) return 'no-webgl'
    const ext = gl.getExtension('WEBGL_debug_renderer_info')
    const raw = ext !== null
      ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
      : gl.getParameter(gl.RENDERER)
    gl.getExtension('WEBGL_lose_context')?.loseContext()
    return typeof raw === 'string' ? raw : 'unknown'
  } catch {
    return 'probe-failed'
  }
}

function probeEnvironment(display: DisplayInfo): EnvFingerprint {
  const nav = navigator as Navigator & { deviceMemory?: number }
  const renderer = probeRenderer()
  return {
    ua: navigator.userAgent,
    platform: navigator.platform,
    dpr: window.devicePixelRatio,
    colorDepth: window.screen.colorDepth,
    screen: { w: window.screen.width, h: window.screen.height },
    viewport: { w: window.innerWidth, h: window.innerHeight },
    hardwareConcurrency: navigator.hardwareConcurrency ?? 0,
    deviceMemoryGiB: typeof nav.deviceMemory === 'number' ? nav.deviceMemory : null,
    reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    renderer,
    software: isSoftwareRenderer(renderer),
    display,
  }
}

// ---------------------------------------------------------------- 帧采样

/** 空闲期采一段 rAF 间隔，用来估显示器刷新率。
 *
 *  **必须在挂载列表之前调用。** 先挂 1000 行再校准，量到的是「这台机器跑这个页面
 *  有多快」，再拿这个数去给这个页面放宽门槛 —— 自我循环论证，而且是往好看的方向。 */
function calibrateDisplay(): Promise<DisplayInfo> {
  return new Promise((resolve) => {
    const deltas: number[] = []
    const start = performance.now()
    let last = 0
    const step = (now: number): void => {
      if (last !== 0) deltas.push(now - last)
      last = now
      if (now - start < CALIBRATE_MS) requestAnimationFrame(step)
      else resolve(estimateRefresh(deltas))
    }
    requestAnimationFrame(step)
  })
}

interface SampleOptions {
  ms: number
  /** null 表示不滚动 */
  scroller: HTMLElement | null
}

/** 逐帧推进 scrollTop 并记录相邻 rAF 时间戳之差。首帧间隔不采。 */
function sampleFrames(options: SampleOptions): Promise<number[]> {
  return new Promise((resolve) => {
    const deltas: number[] = []
    const start = performance.now()
    let last = 0
    const maxScroll = options.scroller === null
      ? 0
      : Math.max(0, options.scroller.scrollHeight - options.scroller.clientHeight)

    const step = (now: number): void => {
      if (last !== 0) deltas.push(now - last)
      last = now
      const elapsed = now - start

      if (options.scroller !== null && maxScroll > 0) {
        // 按时间推进而不是按帧递增：帧率掉了滚动距离也该一致，
        // 否则「掉帧」会让滚动变慢，测出来的压力反而变小
        options.scroller.scrollTop = ((elapsed / 1000) * SCROLL_PX_PER_SECOND) % maxScroll
      }

      if (elapsed < options.ms) requestAnimationFrame(step)
      else resolve(deltas)
    }
    requestAnimationFrame(step)
  })
}

// ---------------------------------------------------------------- 被挂载的舞台

interface StageProps {
  initial: Torrent[]
  height: number
  onReady: (set: (next: Torrent[]) => void) => void
}

function Stage({ initial, height, onReady }: StageProps) {
  const [torrents, setTorrents] = useState<Torrent[]>(initial)
  // layout effect 在 flushSync 的提交阶段同步执行，所以 runScene 返回时
  // setter 一定可用，不需要再多等一帧
  useLayoutEffect(() => {
    onReady(setTorrents)
  }, [])
  return <TorrentList torrents={torrents} viewportHeight={height} />
}

// ---------------------------------------------------------------- 单个场景

async function runScene(
  mount: HTMLElement,
  def: ScenarioDef,
  thresholds: Thresholds,
): Promise<SceneResult> {
  mount.replaceChildren()
  const host = document.createElement('div')
  mount.appendChild(host)

  const initial = makeTorrents(def.torrents)
  const root = createRoot(host)
  const holder: { set: ((next: Torrent[]) => void) | null } = { set: null }

  const t0 = performance.now()
  flushSync(() => {
    root.render(
      <Stage
        initial={initial}
        height={STAGE_HEIGHT}
        onReady={(set) => {
          holder.set = set
        }}
      />,
    )
  })
  const mountMs = performance.now() - t0

  const scroller = host.querySelector<HTMLElement>('.list')
  if (def.scroll && scroller === null) {
    root.unmount()
    throw new Error('找不到滚动容器 .list，样式可能没加载')
  }

  // 首屏合成完成再开始数帧，否则把挂载期的抖动算进滚动场景
  await waitFrames(3)

  let timer: ReturnType<typeof setInterval> | null = null
  if (def.updateHz > 0) {
    const set = holder.set
    if (set === null) {
      root.unmount()
      throw new Error('数据更新 setter 未就绪')
    }
    let tick = 0
    timer = setInterval(() => {
      tick += 1
      // 每 tick 重建全部对象：真实快照就是整份替换，省掉这步等于少测一半成本
      set(
        initial.map((t) => ({
          ...t,
          progress: Math.min(1, t.progress + tick * 0.001),
          rateDown: t.rateDown + tick * 1000,
          rateUp: (t.rateUp + tick * 137) % 1_500_000,
        })),
      )
    }, Math.round(1000 / def.updateHz))
  }

  let deltas: number[] = []
  try {
    deltas = await sampleFrames({
      ms: def.sampleMs,
      scroller: def.scroll ? scroller : null,
    })
  } finally {
    if (timer !== null) clearInterval(timer)
    root.unmount()
  }

  return {
    id: def.id,
    name: def.name,
    stats: summarizeFrames(deltas, thresholds),
    extra: { mountMs: Math.round(mountMs * 100) / 100 },
  }
}

// ---------------------------------------------------------------- 主流程

function describeScene(r: SceneResult): string {
  const s = r.stats
  return [
    `  ${r.name}: ${s.frames} 帧 / ${s.fps}fps`,
    `p50=${s.p50} p95=${s.p95} p99=${s.p99} max=${s.max}ms`,
    `掉帧 ${s.dropped}(${(s.droppedRatio * 100).toFixed(1)}%)`,
    `卡顿 ${s.jank}(${(s.jankRatio * 100).toFixed(1)}%)`,
    `挂载 ${r.extra.mountMs ?? 0}ms`,
  ].join(' · ')
}

function describeDisplay(display: DisplayInfo): string {
  if (display.source === 'unavailable') {
    return `刷新率 未测到（rAF 中位间隔 ${display.medianFrameMs ?? '无'}ms，样本 ${display.samples} 个）`
      + '：页面可能不在前台，本次数据不参与判定'
  }
  return `刷新率 实测 ${display.refreshHz}Hz / 命中 ${display.nominalHz}Hz 档`
    + ` · 判定按 ${display.targetHz}Hz（预算 ${Math.round((1000 / display.targetHz) * 100) / 100}ms）`
    + ` · 中位间隔 ${display.medianFrameMs}ms / 样本 ${display.samples}`
}

async function runAll(log: (line: string) => void): Promise<PerfReport> {
  const mount = must('perf-stage-mount')

  // 校准先于一切：挂上列表之后测到的就不是面板刷新率了
  log('▶ 校准显示器刷新率（空页面 600ms）')
  const display = await calibrateDisplay()
  const env = probeEnvironment(display)

  log(`环境 dpr=${env.dpr} 屏幕=${env.screen.w}x${env.screen.h} 视口=${env.viewport.w}x${env.viewport.h}`)
  log(`渲染器 ${env.renderer}`)
  log(describeDisplay(display))
  if (env.software) {
    log('⚠ 软件光栅化：帧率由渲染器决定，本份数据只作记录，不参与判定')
  }
  if (env.reducedMotion) {
    log('⚠ 系统开启「减少动态效果」：transition 已被禁用，数据偏乐观')
  }
  if (display.source === 'measured' && display.targetHz < 60 - 0.5) {
    log(`⚠ 面板只有 ${display.targetHz}Hz：阈值按它缩放，绝对帧率不能与 60Hz 机器直接比`)
  }

  await document.fonts.ready

  // 一套阈值贯穿三个场景与最终结论，避免「场景按 48Hz 数掉帧、结论按 60Hz 判定」
  const thresholds = thresholdsFor(env)
  const results: SceneResult[] = []
  for (const def of SCENARIOS) {
    log(`▶ ${def.name} — ${def.question}`)
    const result = await runScene(mount, def, thresholds)
    results.push(result)
    log(describeScene(result))
    await waitMs(COOLDOWN_MS)
  }

  const cssAudit = detectAnimationViolations(
    collectCssText(Array.from(document.styleSheets)),
  )
  log(
    cssAudit.length === 0
      ? `样式审计：0 处违规（只动 transform/opacity，无 backdrop-filter）`
      : `样式审计：${cssAudit.length} 处违规 — ${cssAudit.map((v) => v.rule).join(', ')}`,
  )

  const report = buildReport({ env, scenarios: results, cssAudit })
  log(`结论 ${report.verdict.level}`)
  for (const reason of report.verdict.reasons) log(`  · ${reason}`)

  mount.replaceChildren()
  return report
}

// ---------------------------------------------------------------- 结果导出

function fileName(report: PerfReport): string {
  const stamp = report.tookAt.replace(/[:.]/g, '-').replace('T', '_').slice(0, 19)
  return `perf_${stamp}_dpr${report.env.dpr}.json`
}

function saveReport(report: PerfReport): void {
  const blob = new Blob([JSON.stringify(report, null, 2)], {
    type: 'application/json;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName(report)
  a.click()
  // 立刻 revoke 会让部分浏览器的下载中断，给一个宽限
  setTimeout(() => {
    URL.revokeObjectURL(url)
  }, 10_000)
}

/** headless 冒烟用：页面跑完把结果 POST 回本地临时服务器。
 *  普通打开时没有 report 参数，这段完全不执行 —— 包仍然零网络依赖。 */
async function maybePost(report: PerfReport): Promise<string> {
  const target = new URLSearchParams(window.location.search).get('report')
  if (target === null || target === '') return '（未指定回传地址，跳过）'
  try {
    const resp = await fetch(target, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(report),
    })
    return resp.ok ? `已回传 ${target}` : `回传失败 HTTP ${resp.status}`
  } catch (error) {
    return `回传异常 ${error instanceof Error ? error.message : String(error)}`
  }
}

// ---------------------------------------------------------------- 装配

function main(): void {
  const logPre = must('perf-log')
  const statusEl = must('perf-status')
  const runBtn = document.getElementById('perf-run') as HTMLButtonElement | null
  const saveBtn = document.getElementById('perf-save') as HTMLButtonElement | null
  const envEl = must('perf-env')
  const reportPre = must('perf-report')
  const log = makeLogger(logPre)

  // 刷新率要等点了「开始测量」才采得到，这里先按「未测到」占位
  const env = probeEnvironment(estimateRefresh([]))
  envEl.textContent = env.software
    ? `渲染器 ${env.renderer}（软件光栅化）· dpr ${env.dpr} · ${env.viewport.w}x${env.viewport.h}`
    : `渲染器 ${env.renderer} · dpr ${env.dpr} · ${env.viewport.w}x${env.viewport.h} · ${env.hardwareConcurrency} 线程`

  let latest: PerfReport | null = null

  const run = async (): Promise<void> => {
    if (runBtn !== null) runBtn.disabled = true
    if (saveBtn !== null) saveBtn.disabled = true
    statusEl.textContent = '测量中…'
    statusEl.dataset['level'] = ''
    logPre.textContent = ''
    latest = null

    try {
      const report = await runAll(log)
      latest = report
      statusEl.textContent = `结论：${report.verdict.level}`
      statusEl.dataset['level'] = report.verdict.level
      // 跑完才知道真实刷新率，顶部那行要跟着更新
      envEl.textContent = `渲染器 ${report.env.renderer} · dpr ${report.env.dpr}`
        + ` · ${report.env.viewport.w}x${report.env.viewport.h}`
        + ` · ${describeDisplay(report.env.display)}`
      if (saveBtn !== null) saveBtn.disabled = false
      // 同时写进 DOM：headless 抓不到下载，但能抓到这份文本
      reportPre.textContent = JSON.stringify(report)
      log(await maybePost(report))
    } catch (error) {
      statusEl.textContent = '测量失败'
      statusEl.dataset['level'] = 'fail'
      log(`异常：${error instanceof Error ? `${error.message}\n${error.stack ?? ''}` : String(error)}`)
    } finally {
      if (runBtn !== null) runBtn.disabled = false
    }
  }

  runBtn?.addEventListener('click', () => {
    void run()
  })
  saveBtn?.addEventListener('click', () => {
    if (latest !== null) saveReport(latest)
  })

  document.documentElement.dataset['perfReady'] = '1'

  // 只在显式要求时自动跑（headless 冒烟用），避免正常打开就自己动
  if (new URLSearchParams(window.location.search).get('auto') === '1') {
    void run()
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main, { once: true })
} else {
  main()
}
