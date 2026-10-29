/** 帧率探针的纯函数层。
 *
 * 单独成文件是为了「能被单测判定」：统计口径、违规识别、结论分档这三件事
 * 一旦混进 React 组件或 DOM 操作里，就只能靠肉眼看，跨机数据也没法互相比较。
 *
 * 口径约定（跨机数据可比的前提，改动等于让历史数据失效）：
 *   - 帧预算 16.67ms（60Hz）
 *   - 掉帧 = delta > 1.5 × 预算（25ms），即错过一个合成周期
 *   - 卡顿 = delta > 2 × 预算（33.3ms），即连续错过两帧
 *   - 首帧 delta 不采（rAF 启动到第一帧的间隔不是渲染耗时）
 *   - 分位数用线性插值
 */

import type { Torrent } from './api'

export const FRAME_BUDGET_MS = 1000 / 60

export const THRESHOLDS = {
  budgetMs: FRAME_BUDGET_MS,
  /** 超过这个算掉帧 */
  dropMs: FRAME_BUDGET_MS * 1.5,
  /** 超过这个算卡顿 */
  jankMs: FRAME_BUDGET_MS * 2,
  /** 场景 p95 超过这个值即记一条问题 */
  p95MaxMs: 20,
  /** 卡顿帧占比上限 */
  jankRatioMax: 0.01,
  /** S3 相对 S2 的 fps 下降上限 */
  s3FpsDropMax: 0.15,
  /** 低于这个 fps 直接判 fail 而不是 warn */
  failFps: 30,
  /** p95 高于这个值直接判 fail */
  failP95Ms: 40,
} as const

export interface FrameStats {
  /** 参与统计的帧数 */
  frames: number
  durationMs: number
  fps: number
  p50: number
  p95: number
  p99: number
  max: number
  dropped: number
  droppedRatio: number
  jank: number
  jankRatio: number
}

/** 线性插值分位数。输入必须已升序。 */
export function percentile(sorted: readonly number[], p: number): number {
  const n = sorted.length
  if (n === 0) return 0
  if (n === 1) return sorted[0]
  if (p <= 0) return sorted[0]
  if (p >= 1) return sorted[n - 1]
  const idx = (n - 1) * p
  const lo = Math.floor(idx)
  const hi = Math.ceil(idx)
  if (lo === hi) return sorted[lo]
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo)
}

function round(value: number, digits = 2): number {
  const f = 10 ** digits
  return Math.round(value * f) / f
}

/** 把逐帧间隔汇总成统计量。空输入返回全零而不是 NaN —— NaN 会让 JSON 变成 null。 */
export function summarizeFrames(deltas: readonly number[]): FrameStats {
  const frames = deltas.length
  if (frames === 0) {
    return {
      frames: 0,
      durationMs: 0,
      fps: 0,
      p50: 0,
      p95: 0,
      p99: 0,
      max: 0,
      dropped: 0,
      droppedRatio: 0,
      jank: 0,
      jankRatio: 0,
    }
  }

  const sorted = [...deltas].sort((a, b) => a - b)
  const durationMs = deltas.reduce((sum, d) => sum + d, 0)
  let dropped = 0
  let jank = 0
  for (const d of deltas) {
    if (d > THRESHOLDS.dropMs) dropped += 1
    if (d > THRESHOLDS.jankMs) jank += 1
  }

  return {
    frames,
    durationMs: round(durationMs),
    fps: durationMs > 0 ? round((frames * 1000) / durationMs) : 0,
    p50: round(percentile(sorted, 0.5)),
    p95: round(percentile(sorted, 0.95)),
    p99: round(percentile(sorted, 0.99)),
    max: round(sorted[sorted.length - 1]),
    dropped,
    droppedRatio: round(dropped / frames, 4),
    jank,
    jankRatio: round(jank / frames, 4),
  }
}

// ---------------------------------------------------------------- 场景矩阵

export type ScenarioId = 's1' | 's2' | 's3'

export interface ScenarioDef {
  id: ScenarioId
  name: string
  torrents: number
  /** 采样时长。滚动与否由 scroll 决定，两者独立取值 */
  sampleMs: number
  /** 采样期间是否驱动滚动 */
  scroll: boolean
  /** 每秒数据更新次数，0 表示不更新 */
  updateHz: number
  /** 这个场景要回答的问题 —— 写出来是为了防止「跑了一堆数不知道该看哪个」 */
  question: string
}

export const SCENARIOS: readonly ScenarioDef[] = [
  {
    id: 's1',
    name: '静态渲染',
    torrents: 1000,
    sampleMs: 1000,
    scroll: false,
    updateHz: 0,
    question: '1000 条挂载后静置是否稳定 60fps？若有持续掉帧说明有空转动画或布局抖动',
  },
  {
    id: 's2',
    name: '纯滚动',
    torrents: 1000,
    sampleMs: 3000,
    scroll: true,
    updateHz: 0,
    question: '只滚动不更新数据时，虚拟滚动与合成层本身的开销是多少',
  },
  {
    id: 's3',
    name: '滚动 + 10Hz 更新',
    torrents: 1000,
    sampleMs: 3000,
    scroll: true,
    updateHz: 10,
    question: '10Hz 快照更新是否干扰滚动 —— 这是真实使用场景（边下载边滚）',
  },
] as const

// ---------------------------------------------------------------- CSS 静态审计

export type ViolationRule =
  | 'layout-transition'
  | 'all-transition'
  | 'will-change-layout'
  | 'backdrop-filter'

export interface Violation {
  rule: ViolationRule
  /** 命中处的原始文本片段，便于定位 */
  snippet: string
}

/** 这些属性改一次就要重排，不能出现在 transition/animation 里 */
const LAYOUT_PROPS = new Set([
  'width',
  'height',
  'top',
  'left',
  'right',
  'bottom',
  'margin',
  'margin-top',
  'margin-right',
  'margin-bottom',
  'margin-left',
  'padding',
  'padding-top',
  'padding-right',
  'padding-bottom',
  'padding-left',
  'border-width',
  'font-size',
  'line-height',
  'flex-basis',
  'gap',
])

function propNames(value: string): string[] {
  // `background-color 150ms ease, transform 200ms var(--ease)` → ['background-color','transform']
  return value
    .split(',')
    .map((part) => part.trim().split(/\s+/)[0] ?? '')
    .map((name) => name.toLowerCase())
    .filter((name) => name.length > 0)
}

/** 扫 CSS 文本里的布局动画违规。返回空数组表示合规。 */
export function detectAnimationViolations(cssText: string): Violation[] {
  const out: Violation[] = []

  const transitionRe = /transition(?:-property)?\s*:\s*([^;}]+)/gi
  for (const m of cssText.matchAll(transitionRe)) {
    const value = m[1]
    for (const name of propNames(value)) {
      if (name === 'all') {
        out.push({ rule: 'all-transition', snippet: m[0].trim() })
      } else if (LAYOUT_PROPS.has(name)) {
        out.push({ rule: 'layout-transition', snippet: m[0].trim() })
      }
    }
  }

  const willChangeRe = /will-change\s*:\s*([^;}]+)/gi
  for (const m of cssText.matchAll(willChangeRe)) {
    for (const name of propNames(m[1])) {
      if (name !== 'transform' && name !== 'opacity') {
        out.push({ rule: 'will-change-layout', snippet: m[0].trim() })
      }
    }
  }

  // 出现即记：滚动时实时模糊必然掉帧，没有「用得少就没事」这一说
  const backdropRe = /(?:-webkit-)?backdrop-filter\s*:/gi
  for (const m of cssText.matchAll(backdropRe)) {
    out.push({ rule: 'backdrop-filter', snippet: m[0].trim() })
  }

  return out
}

/** 从样式表集合里取出全部规则文本。跨域样式表读不到 cssRules，静默跳过。 */
export function collectCssText(sheets: readonly CSSStyleSheet[]): string {
  const parts: string[] = []
  for (const sheet of sheets) {
    try {
      for (const rule of Array.from(sheet.cssRules)) parts.push(rule.cssText)
    } catch {
      // 跨域样式表访问 cssRules 会抛 SecurityError，跳过即可
    }
  }
  return parts.join('\n')
}

// ---------------------------------------------------------------- 环境指纹

export interface EnvFingerprint {
  ua: string
  platform: string
  dpr: number
  colorDepth: number
  screen: { w: number; h: number }
  viewport: { w: number; h: number }
  hardwareConcurrency: number
  /** 浏览器不支持时为 null，不用 undefined —— JSON 会丢掉 undefined 导致往返不相等 */
  deviceMemoryGiB: number | null
  reducedMotion: boolean
  /** WebGL renderer 字符串；拿不到时为 'unknown' */
  renderer: string
  software: boolean
}

/** 软件光栅化特征串。命中则帧率数据不可用于判定。 */
const SOFTWARE_HINTS = [
  'swiftshader',
  'basic render',
  'microsoft basic',
  'llvmpipe',
  'softpipe',
  'software',
]

export function isSoftwareRenderer(renderer: string): boolean {
  const lower = renderer.toLowerCase()
  return SOFTWARE_HINTS.some((hint) => lower.includes(hint))
}

// ---------------------------------------------------------------- 结论

export type VerdictLevel = 'pass' | 'warn' | 'fail' | 'env-limited'

export interface Verdict {
  level: VerdictLevel
  reasons: string[]
}

export interface SceneResult {
  id: ScenarioId
  name: string
  stats: FrameStats
  /** 场景自定义指标（S1 的挂载耗时等），必须是可 JSON 化的原始值 */
  extra: Record<string, number>
}

export interface PerfReport {
  schema: 'perf-v1'
  tookAt: string
  env: EnvFingerprint
  scenarios: SceneResult[]
  cssAudit: Violation[]
  thresholds: typeof THRESHOLDS
  verdict: Verdict
}

function findScene(scenarios: readonly SceneResult[], id: ScenarioId): SceneResult | null {
  return scenarios.find((s) => s.id === id) ?? null
}

/** 按阈值给结论。每条 reason 都要能被追溯到具体场景与具体数字。 */
export function classifyVerdict(
  env: EnvFingerprint,
  scenarios: readonly SceneResult[],
): Verdict {
  if (env.software) {
    return {
      level: 'env-limited',
      reasons: [
        `检测到软件光栅化（renderer=${env.renderer}），帧率由渲染器决定而非页面代码，数据不参与判定`,
      ],
    }
  }

  const s2 = findScene(scenarios, 's2')
  const s3 = findScene(scenarios, 's3')
  if (s2 === null || s3 === null) {
    return {
      level: 'fail',
      reasons: ['场景未跑完（缺 s2 或 s3），无法给出结论'],
    }
  }

  const reasons: string[] = []
  let hard = false

  for (const scene of scenarios) {
    const { stats, name } = scene
    if (stats.frames === 0) {
      reasons.push(`${name}：没有采到任何帧，rAF 可能被浏览器节流（页面在后台？）`)
      hard = true
      continue
    }
    if (stats.p95 > THRESHOLDS.failP95Ms) {
      reasons.push(`${name}：p95 ${stats.p95}ms 超过 ${THRESHOLDS.failP95Ms}ms`)
      hard = true
    } else if (stats.p95 > THRESHOLDS.p95MaxMs) {
      reasons.push(`${name}：p95 ${stats.p95}ms 超过 ${THRESHOLDS.p95MaxMs}ms`)
    }
    if (stats.fps < THRESHOLDS.failFps) {
      reasons.push(`${name}：${stats.fps}fps 低于 ${THRESHOLDS.failFps}fps`)
      hard = true
    }
    if (stats.jankRatio > THRESHOLDS.jankRatioMax) {
      reasons.push(
        `${name}：卡顿帧占比 ${(stats.jankRatio * 100).toFixed(1)}% 超过 ${(THRESHOLDS.jankRatioMax * 100).toFixed(0)}%`,
      )
    }
  }

  if (s2.stats.fps > 0 && s3.stats.fps > 0) {
    const drop = (s2.stats.fps - s3.stats.fps) / s2.stats.fps
    if (drop > THRESHOLDS.s3FpsDropMax) {
      reasons.push(
        `s3 相对 s2 掉速 ${(drop * 100).toFixed(1)}% 超过 ${(THRESHOLDS.s3FpsDropMax * 100).toFixed(0)}%：数据更新正在干扰滚动`,
      )
    }
  }

  if (reasons.length === 0) return { level: 'pass', reasons: ['所有场景在阈值内'] }
  return { level: hard ? 'fail' : 'warn', reasons }
}

export interface BuildReportInput {
  env: EnvFingerprint
  scenarios: SceneResult[]
  cssAudit: Violation[]
  /** 注入固定时间戳，便于单测做 JSON 往返 */
  tookAt?: string
}

export function buildReport(input: BuildReportInput): PerfReport {
  return {
    schema: 'perf-v1',
    tookAt: input.tookAt ?? new Date().toISOString(),
    env: input.env,
    scenarios: input.scenarios,
    cssAudit: input.cssAudit,
    thresholds: THRESHOLDS,
    verdict: classifyVerdict(input.env, input.scenarios),
  }
}

/** 生成假数据用的稳定伪随机发生器：同一 seed 跨机产出一致，排除数据本身的影响。 */
export function makeTorrents(count: number, seed = 0x2f6e2b1): Torrent[] {
  let state = seed >>> 0
  const next = (): number => {
    // xorshift32
    state ^= state << 13
    state >>>= 0
    state ^= state >>> 17
    state ^= state << 5
    state >>>= 0
    return state
  }
  const states = ['downloading', 'seeding', 'paused', 'checking', 'queued'] as const
  return Array.from({ length: count }, (_, i) => {
    const r = next()
    const progress = (r % 1000) / 1000
    return {
      infohash: `ih${i.toString(16).padStart(38, '0')}`,
      name: `示例任务 ${i} — ubuntu-24.04-desktop-amd64.iso`,
      state: states[r % states.length],
      paused: r % 7 === 0,
      progress,
      rateDown: r % 8_000_000,
      rateUp: (r >>> 3) % 1_500_000,
      peers: r % 60,
      seeds: (r >>> 4) % 40,
      doneBytes: Math.trunc(progress * 2_000_000_000),
      totalBytes: 2_000_000_000,
      etaS: progress >= 1 ? null : Math.trunc(3600 * (1 - progress)),
      hasMetadata: true,
      error: null,
    }
  })
}
