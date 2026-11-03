/** 帧率探针的纯函数层。
 *
 * 单独成文件是为了「能被单测判定」：统计口径、违规识别、结论分档这三件事
 * 一旦混进 React 组件或 DOM 操作里，就只能靠肉眼看，跨机数据也没法互相比较。
 *
 * 口径约定（跨机数据可比的前提，改动等于让历史数据失效）：
 *   - 帧预算由**实测显示器刷新率**推导，不再写死 16.67ms（见 estimateRefresh）
 *   - 判定目标帧率 = min(60, 面板刷新率)：面板更低时按面板算，更高时不提高门槛
 *   - 掉帧 = delta > 1.5 × 预算，即错过一个合成周期
 *   - 卡顿 = delta > 2 × 预算，即连续错过两帧
 *   - 中位帧 > 1.5 × 预算直接判 fail：一半的帧都在漏 vsync，与百分比无关
 *   - 首帧 delta 不采（rAF 启动到第一帧的间隔不是渲染耗时）
 *   - 分位数用线性插值
 */

import type { Torrent } from './api'

/** 60Hz 的帧预算。保留这个常量只为「换算成 60Hz 尺子时」有个可读的名字，
 *  实际判定一律走 deriveThresholds()，不要直接拿它算。 */
export const FRAME_BUDGET_MS = 1000 / 60

/** 判定用的产品目标帧率。面板比它低时按面板算，比它高时不提高门槛。 */
export const TARGET_HZ = 60

export interface Thresholds {
  /** 本次判定实际采用的目标帧率（min(60, 面板刷新率)） */
  targetHz: number
  /** 一帧的时间片 */
  budgetMs: number
  /** 超过这个算掉帧 */
  dropMs: number
  /** 超过这个算卡顿 */
  jankMs: number
  /** 中位帧间隔上限：超过即「一半的帧都漏了 vsync」 */
  p50MaxMs: number
  /** 场景 p95 超过这个值即记一条问题 */
  p95MaxMs: number
  /** 卡顿帧占比上限 */
  jankRatioMax: number
  /** S3 相对 S2 的 fps 下降上限 */
  s3FpsDropMax: number
  /** 低于这个 fps 直接判 fail 而不是 warn */
  failFps: number
  /** p95 高于这个值直接判 fail */
  failP95Ms: number
}

/** 按目标帧率推导整套阈值。所有判定线都从这一个数长出来，
 *  改一处即可整体缩放，不会出现「p95 按 48Hz 算、掉帧按 60Hz 算」。 */
export function deriveThresholds(targetHz: number): Thresholds {
  const hz = Number.isFinite(targetHz) && targetHz >= 15 && targetHz <= TARGET_HZ
    ? targetHz
    : TARGET_HZ
  const budget = 1000 / hz
  return {
    targetHz: hz,
    budgetMs: budget,
    dropMs: budget * 1.5,
    jankMs: budget * 2,
    p50MaxMs: budget * 1.5,
    // 60Hz 下等于历史口径的 20ms / 40ms，48Hz 下放宽到 25ms / 50ms
    p95MaxMs: round(budget * 1.2, 1),
    jankRatioMax: 0.01,
    s3FpsDropMax: 0.15,
    failFps: round(hz * 0.5, 1),
    failP95Ms: round(budget * 2.4, 1),
  }
}

/** 60Hz 基准阈值。历史数据与文档都按它对齐，新代码请用 thresholdsFor(env)。 */
export const THRESHOLDS: Thresholds = deriveThresholds(TARGET_HZ)

// ---------------------------------------------------------------- 显示器刷新率

/** 常见刷新率档位。实测中位数几乎不可能是整数毫秒（19.98 / 20.83 / 33.42），
 *  不吸附的话「48.08Hz」这种数字会让下游判定跟着抖动。 */
const NOMINAL_HZ = [24, 25, 30, 40, 48, 50, 60, 72, 75, 90, 100, 120, 144, 165, 240] as const

/** 吸附容差（相对误差）。58.8Hz → 60，48.08Hz → 48，71.4Hz → 72。 */
const SNAP_TOLERANCE = 0.05

/** 少于这么多帧的校准窗口没有统计意义（600ms 在 48Hz 下约 29 个间隔） */
const MIN_CALIBRATION_SAMPLES = 5

/** 中位间隔超过这个值就认为 rAF 已被节流（页面在后台），刷新率无从谈起 */
const THROTTLED_FRAME_MS = 100

export interface DisplayInfo {
  /** 实测刷新率，一位小数；测不到为 null */
  refreshHz: number | null
  /** 吸附到常见档位后的值；没有足够接近的档位时等于 refreshHz */
  nominalHz: number | null
  /** 校准窗口内 rAF 间隔的中位数 */
  medianFrameMs: number | null
  /** 参与校准的间隔数 */
  samples: number
  /** measured = 可信；unavailable = 页面被节流或样本不足，数据不参与判定 */
  source: 'measured' | 'unavailable'
  /** 判定目标帧率，永远有值（不可信时回落到 60） */
  targetHz: number
}

function snapNominal(hz: number): number {
  let best: number = NOMINAL_HZ[0]
  let bestErr = Number.POSITIVE_INFINITY
  for (const candidate of NOMINAL_HZ) {
    const err = Math.abs(candidate - hz) / candidate
    if (err < bestErr) {
      bestErr = err
      best = candidate
    }
  }
  return bestErr <= SNAP_TOLERANCE ? best : hz
}

function unavailableDisplay(samples: number, medianFrameMs: number | null): DisplayInfo {
  return {
    refreshHz: null,
    nominalHz: null,
    medianFrameMs,
    samples,
    source: 'unavailable',
    targetHz: TARGET_HZ,
  }
}

/** 由空闲期的 rAF 间隔估计显示器刷新率。
 *
 *  **必须中位数，不能用均值。** 一次 200ms 的 GC 停顿会把 30 个样本的均值拉低一倍，
 *  于是把一台 60Hz 的机器判成 45Hz，门槛跟着松掉 —— 这是最坏的错法：它让数据变好看。
 *
 *  **必须在空页面上采。** 若先挂上 1000 行列表再校准，量到的是「这台机器跑这个页面
 *  有多快」，再拿它去给这个页面开脱，等于自我循环论证。
 */
export function estimateRefresh(deltas: readonly number[]): DisplayInfo {
  const usable = deltas.filter((d) => Number.isFinite(d) && d > 0)
  if (usable.length < MIN_CALIBRATION_SAMPLES) {
    return unavailableDisplay(usable.length, null)
  }
  const sorted = [...usable].sort((a, b) => a - b)
  const median = percentile(sorted, 0.5)
  if (median > THROTTLED_FRAME_MS) {
    // 页面在后台 / 窗口被完全遮挡时 rAF 会掉到 1Hz 级别，这时「刷新率」是伪值
    return unavailableDisplay(usable.length, round(median))
  }
  const refreshHz = round(1000 / median, 1)
  const nominalHz = round(snapNominal(refreshHz), 1)
  return {
    refreshHz,
    nominalHz,
    medianFrameMs: round(median),
    samples: usable.length,
    source: 'measured',
    targetHz: Math.min(TARGET_HZ, nominalHz),
  }
}

/** 报告里没采到 display 时（老版本产出的 JSON、或手写样本）按 60Hz 处理，不抛错。 */
export function thresholdsFor(env: EnvFingerprint): Thresholds {
  const display = (env as { display?: DisplayInfo }).display ?? unavailableDisplay(0, null)
  return deriveThresholds(display.targetHz)
}


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

/** 把逐帧间隔汇总成统计量。空输入返回全零而不是 NaN —— NaN 会让 JSON 变成 null。
 *
 *  掉帧/卡顿的判定线来自 thresholds：48Hz 面板上「错过一个合成周期」是 31ms 而不是 25ms。
 *  不传时按 60Hz 基准算，保证老数据口径不变。 */
export function summarizeFrames(
  deltas: readonly number[],
  thresholds: Thresholds = THRESHOLDS,
): FrameStats {
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
    if (d > thresholds.dropMs) dropped += 1
    if (d > thresholds.jankMs) jank += 1
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
  /** 实测显示器刷新率与由此推导的判定档位 */
  display: DisplayInfo
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
  schema: 'perf-v2'
  tookAt: string
  env: EnvFingerprint
  scenarios: SceneResult[]
  cssAudit: Violation[]
  /** 本次判定实际使用的阈值（由 display.targetHz 推导，不是常量） */
  thresholds: Thresholds
  verdict: Verdict
}

function findScene(scenarios: readonly SceneResult[], id: ScenarioId): SceneResult | null {
  return scenarios.find((s) => s.id === id) ?? null
}

/** 按阈值给结论。每条 reason 都要能被追溯到具体场景与具体数字。
 *
 *  阈值不是常量，而是从 env.display 推导：一块 48Hz 的屏上稳定 48fps 是正常的，
 *  同一份数据在 60Hz 的尺子下却会被判 warn —— 那是尺子的问题，不是代码的问题。 */
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

  const display = (env as { display?: DisplayInfo }).display ?? unavailableDisplay(0, null)
  if (display.source === 'unavailable') {
    return {
      level: 'env-limited',
      reasons: [
        `没能测到显示器刷新率（rAF 中位间隔 ${display.medianFrameMs ?? '无'}ms，样本 ${display.samples} 个）：`
          + '页面可能不在前台、或 rAF 被节流。帧率口径无法确定，数据不参与判定',
      ],
    }
  }

  const t = deriveThresholds(display.targetHz)

  const s2 = findScene(scenarios, 's2')
  const s3 = findScene(scenarios, 's3')
  if (s2 === null || s3 === null) {
    return {
      level: 'fail',
      reasons: ['场景未跑完（缺 s2 或 s3），无法给出结论'],
    }
  }

  const notes: string[] = []
  const problems: string[] = []
  let hard = false

  if (t.targetHz < TARGET_HZ - 0.5) {
    notes.push(
      `显示器实测 ${display.refreshHz}Hz（命中 ${t.targetHz}Hz 档），阈值已按 ${t.targetHz}Hz 缩放`
        + `（帧预算 ${round(t.budgetMs, 2)}ms）：绝对帧率不能与 60Hz 机器直接对比`,
    )
  }

  for (const scene of scenarios) {
    const { stats, name } = scene
    if (stats.frames === 0) {
      problems.push(`${name}：没有采到任何帧，rAF 可能被浏览器节流（页面在后台？）`)
      hard = true
      continue
    }
    // 中位帧就超预算 = 一半以上的帧在漏 vsync。这条与百分比无关，
    // 是防止「面板越低、门槛越松，退化反而被放过」的唯一手段。
    if (stats.p50 > t.p50MaxMs) {
      problems.push(
        `${name}：中位帧间隔 ${stats.p50}ms 超过 ${round(t.p50MaxMs, 1)}ms`
          + `（${t.targetHz}Hz 预算的 1.5 倍）—— 一半以上的帧都在漏 vsync`,
      )
      hard = true
    } else if (stats.p95 > t.failP95Ms) {
      problems.push(`${name}：p95 ${stats.p95}ms 超过 ${t.failP95Ms}ms`)
      hard = true
    } else if (stats.p95 > t.p95MaxMs) {
      problems.push(`${name}：p95 ${stats.p95}ms 超过 ${t.p95MaxMs}ms`)
    }
    if (stats.fps < t.failFps) {
      problems.push(`${name}：${stats.fps}fps 低于 ${t.failFps}fps`)
      hard = true
    }
    if (stats.jankRatio > t.jankRatioMax) {
      problems.push(
        `${name}：卡顿帧占比 ${(stats.jankRatio * 100).toFixed(1)}% 超过 ${(t.jankRatioMax * 100).toFixed(0)}%`,
      )
    }
  }

  if (s2.stats.fps > 0 && s3.stats.fps > 0) {
    const drop = (s2.stats.fps - s3.stats.fps) / s2.stats.fps
    if (drop > t.s3FpsDropMax) {
      problems.push(
        `s3 相对 s2 掉速 ${(drop * 100).toFixed(1)}% 超过 ${(t.s3FpsDropMax * 100).toFixed(0)}%：数据更新正在干扰滚动`,
      )
    }
  }

  if (problems.length === 0) {
    return { level: 'pass', reasons: [...notes, '所有场景在阈值内'] }
  }
  return { level: hard ? 'fail' : 'warn', reasons: [...notes, ...problems] }
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
    schema: 'perf-v2',
    tookAt: input.tookAt ?? new Date().toISOString(),
    env: input.env,
    scenarios: input.scenarios,
    cssAudit: input.cssAudit,
    thresholds: thresholdsFor(input.env),
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
