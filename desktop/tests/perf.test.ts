/** 帧率探针纯函数层的判定。
 *
 * 这个文件的存在意义是：让「统计口径」和「结论分档」变成可回归的断言，
 * 而不是写在页面里靠人眼看数字。跨机采回来的数据能不能互相比较，
 * 取决于这里的口径有没有漂移。
 */

import { describe, expect, it } from 'vitest'
import projectCss from '../src/styles.css?raw'
import {
  FRAME_BUDGET_MS,
  SCENARIOS,
  THRESHOLDS,
  buildReport,
  classifyVerdict,
  collectCssText,
  detectAnimationViolations,
  isSoftwareRenderer,
  makeTorrents,
  percentile,
  summarizeFrames,
} from '../src/perf-harness'
import type { EnvFingerprint, SceneResult } from '../src/perf-harness'

function fakeEnv(overrides: Partial<EnvFingerprint> = {}): EnvFingerprint {
  return {
    ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) test',
    platform: 'Win32',
    dpr: 1,
    colorDepth: 24,
    screen: { w: 1920, h: 1080 },
    viewport: { w: 1280, h: 720 },
    hardwareConcurrency: 16,
    deviceMemoryGiB: 16,
    reducedMotion: false,
    renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)',
    software: false,
    ...overrides,
  }
}

function scene(id: 's1' | 's2' | 's3', deltas: number[]): SceneResult {
  const def = SCENARIOS.find((s) => s.id === id)
  if (!def) throw new Error(`未知场景 ${id}`)
  return { id, name: def.name, stats: summarizeFrames(deltas), extra: {} }
}

/** 生成 n 个稳定在 target 毫秒的帧间隔 */
function steady(n: number, target: number): number[] {
  return Array.from({ length: n }, () => target)
}

describe('AC-01 帧差统计的四类边界', () => {
  it('空序列返回全零，不产生 NaN', () => {
    const s = summarizeFrames([])
    expect(s.frames).toBe(0)
    expect(s.fps).toBe(0)
    expect(s.p95).toBe(0)
    // NaN 会被 JSON.stringify 写成 null，污染跨机报告
    for (const v of [s.durationMs, s.fps, s.p50, s.p95, s.p99, s.max]) {
      expect(Number.isNaN(v)).toBe(false)
    }
  })

  it('单帧：所有分位数等于该帧，fps 由单帧间隔推得', () => {
    const s = summarizeFrames([20])
    expect(s.frames).toBe(1)
    expect(s.p50).toBe(20)
    expect(s.p95).toBe(20)
    expect(s.p99).toBe(20)
    expect(s.max).toBe(20)
    expect(s.fps).toBe(50)
    expect(s.dropped).toBe(0) // 20ms 未超过 25ms 掉帧线
  })

  it('全流畅：60Hz 稳定帧无掉帧无卡顿', () => {
    const s = summarizeFrames(steady(180, FRAME_BUDGET_MS))
    expect(s.frames).toBe(180)
    expect(s.fps).toBe(60)
    expect(s.dropped).toBe(0)
    expect(s.jank).toBe(0)
    expect(s.p95).toBe(16.67)
  })

  it('全掉帧：每帧都超过卡顿线，比例饱和到 1', () => {
    const s = summarizeFrames(steady(60, 50))
    expect(s.dropped).toBe(60)
    expect(s.jank).toBe(60)
    expect(s.droppedRatio).toBe(1)
    expect(s.jankRatio).toBe(1)
    expect(s.fps).toBe(20)
  })

  it('分位数用线性插值，不是取整取下标', () => {
    // 0..99 共 100 个点：p50 应落在 49 与 50 之间（线性插值 = 49.5）
    const sorted = Array.from({ length: 100 }, (_, i) => i)
    expect(percentile(sorted, 0.5)).toBe(49.5)
    expect(percentile(sorted, 0)).toBe(0)
    expect(percentile(sorted, 1)).toBe(99)
    expect(percentile([], 0.5)).toBe(0)
  })

  it('掉帧与卡顿用两条不同的线，落在中间的帧只算掉帧', () => {
    // 30ms：> 25ms(掉帧线) 但 < 33.3ms(卡顿线)
    const s = summarizeFrames([30, 30, 30])
    expect(s.dropped).toBe(3)
    expect(s.jank).toBe(0)
  })
})

describe('AC-02 场景矩阵', () => {
  it('恰为三个场景，id 依次 s1/s2/s3', () => {
    expect(SCENARIOS).toHaveLength(3)
    expect(SCENARIOS.map((s) => s.id)).toEqual(['s1', 's2', 's3'])
  })

  it('每个场景都有独立的判定目标（question 非空且互不相同）', () => {
    for (const s of SCENARIOS) {
      expect(s.question.length).toBeGreaterThan(10)
    }
    expect(new Set(SCENARIOS.map((s) => s.question)).size).toBe(3)
  })

  it('分离变量：s1 不滚不更新，s2 只滚，s3 滚+更新', () => {
    const [s1, s2, s3] = SCENARIOS
    expect(s1.scroll).toBe(false)
    expect(s1.updateHz).toBe(0)
    expect(s2.scroll).toBe(true)
    expect(s2.updateHz).toBe(0)
    expect(s3.scroll).toBe(true)
    expect(s3.updateHz).toBe(10)
  })

  it('每个场景都有正采样时长', () => {
    for (const s of SCENARIOS) expect(s.sampleMs).toBeGreaterThan(0)
  })

  it('三场景的任务数一致，只变一个变量', () => {
    expect(new Set(SCENARIOS.map((s) => s.torrents)).size).toBe(1)
  })
})

describe('AC-03 布局动画违规识别', () => {
  it('transition 到布局属性：违规', () => {
    const v = detectAnimationViolations('.a { transition: width 200ms ease; }')
    expect(v.map((x) => x.rule)).toContain('layout-transition')
  })

  it('transition 到 all：违规（隐性包含布局属性）', () => {
    const v = detectAnimationViolations('.a { transition: all 150ms; }')
    expect(v.map((x) => x.rule)).toContain('all-transition')
  })

  it('transition-property 写法同样识别', () => {
    const v = detectAnimationViolations('.a { transition-property: height; }')
    expect(v.map((x) => x.rule)).toContain('layout-transition')
  })

  it('只动 transform/opacity/颜色：合规', () => {
    const v = detectAnimationViolations(
      '.a { transition: transform 200ms var(--ease), opacity 150ms; }',
    )
    expect(v).toEqual([])
  })

  it('will-change 只允许 transform 与 opacity', () => {
    expect(detectAnimationViolations('.a { will-change: transform; }')).toEqual([])
    expect(detectAnimationViolations('.a { will-change: opacity; }')).toEqual([])
    const bad = detectAnimationViolations('.a { will-change: top; }')
    expect(bad.map((x) => x.rule)).toEqual(['will-change-layout'])
  })

  it('backdrop-filter 出现即违规，含 -webkit- 前缀', () => {
    expect(detectAnimationViolations('.a { backdrop-filter: blur(8px); }')).toHaveLength(1)
    expect(detectAnimationViolations('.a { -webkit-backdrop-filter: blur(8px); }')).toHaveLength(1)
  })

  it('项目真实样式表 styles.css 零违规 —— 这条是 M2a 性能约束的机器判定', () => {
    expect(detectAnimationViolations(projectCss)).toEqual([])
  })

  it('collectCssText 对空输入返回空串', () => {
    expect(collectCssText([])).toBe('')
  })
})

describe('AC-04 报告结构可 JSON 往返', () => {
  const scenarios = [
    scene('s1', steady(60, 16.67)),
    scene('s2', steady(180, 16.67)),
    scene('s3', steady(180, 16.67)),
  ]

  it('含 ua / dpr / screen / scenarios / verdict', () => {
    const report = buildReport({ env: fakeEnv(), scenarios, cssAudit: [] })
    expect(typeof report.env.ua).toBe('string')
    expect(typeof report.env.dpr).toBe('number')
    expect(report.env.screen).toEqual({ w: 1920, h: 1080 })
    expect(report.scenarios).toHaveLength(3)
    expect(report.verdict.level).toBe('pass')
    expect(report.schema).toBe('perf-v1')
    expect(report.thresholds).toEqual(THRESHOLDS)
  })

  it('JSON 往返后与原对象深相等（不含 undefined 导致的键丢失）', () => {
    const report = buildReport({ env: fakeEnv(), scenarios, cssAudit: [] })
    const roundTrip = JSON.parse(JSON.stringify(report))
    expect(roundTrip).toEqual(report)
  })

  it('缺 deviceMemory 时写 null 而非 undefined，往返仍相等', () => {
    const report = buildReport({
      env: fakeEnv({ deviceMemoryGiB: null }),
      scenarios,
      cssAudit: [],
    })
    expect(report.env.deviceMemoryGiB).toBeNull()
    expect(JSON.parse(JSON.stringify(report))).toEqual(report)
  })
})

describe('AC-05 结论分档', () => {
  it('软渲染环境判 env-limited，不判 fail', () => {
    const renderers = [
      'Google SwiftShader',
      'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)))',
      'Microsoft Basic Render Driver',
      'llvmpipe (LLVM 15.0.7, 256 bits)',
    ]
    for (const renderer of renderers) {
      expect(isSoftwareRenderer(renderer)).toBe(true)
      const v = classifyVerdict(fakeEnv({ renderer, software: true }), [])
      expect(v.level).toBe('env-limited')
      expect(v.reasons[0]).toContain('软件光栅化')
    }
  })

  it('硬件渲染器不被误判为软渲染', () => {
    expect(isSoftwareRenderer('ANGLE (NVIDIA GeForce RTX 3060 Direct3D11)')).toBe(false)
    expect(isSoftwareRenderer('ANGLE (Intel, Intel(R) UHD Graphics 630)')).toBe(false)
  })

  it('三场景都在阈值内 → pass', () => {
    const v = classifyVerdict(fakeEnv(), [
      scene('s1', steady(60, 16.67)),
      scene('s2', steady(180, 16.67)),
      scene('s3', steady(180, 16.67)),
    ])
    expect(v.level).toBe('pass')
  })

  it('缺场景 → fail，理由指明缺哪个', () => {
    const v = classifyVerdict(fakeEnv(), [scene('s1', steady(60, 16.67))])
    expect(v.level).toBe('fail')
    expect(v.reasons[0]).toContain('s2')
  })

  it('s3 相对 s2 掉速超 15% → 指出是数据更新干扰滚动', () => {
    const v = classifyVerdict(fakeEnv(), [
      scene('s1', steady(60, 16.67)),
      scene('s2', steady(180, 16.67)), // 60fps
      scene('s3', steady(180, 25)), // 40fps
    ])
    expect(v.level).not.toBe('pass')
    expect(v.reasons.join(' ')).toContain('数据更新正在干扰滚动')
  })

  it('fps 低于 30 → fail 而非 warn', () => {
    const v = classifyVerdict(fakeEnv(), [
      scene('s1', steady(60, 16.67)),
      scene('s2', steady(180, 50)), // 20fps
      scene('s3', steady(180, 50)),
    ])
    expect(v.level).toBe('fail')
  })

  it('零帧场景（页面在后台被节流）→ fail 且提示 rAF 未运行', () => {
    const v = classifyVerdict(fakeEnv(), [
      scene('s1', []),
      scene('s2', steady(180, 16.67)),
      scene('s3', steady(180, 16.67)),
    ])
    expect(v.level).toBe('fail')
    expect(v.reasons.join(' ')).toContain('rAF')
  })

  it('p95 轻微超标给 warn，不误报为 fail', () => {
    // 大部分帧 16.67，少量 30ms：p95 落在 25ms 左右
    const deltas = [...steady(170, 16.67), ...steady(10, 30)]
    const v = classifyVerdict(fakeEnv(), [
      scene('s1', steady(60, 16.67)),
      scene('s2', deltas),
      scene('s3', deltas),
    ])
    expect(v.level).toBe('warn')
  })
})

describe('假数据发生器', () => {
  it('1000 条、infohash 唯一、可用作 React key', () => {
    const list = makeTorrents(1000)
    expect(list).toHaveLength(1000)
    expect(new Set(list.map((t) => t.infohash)).size).toBe(1000)
  })

  it('同 seed 跨机产出一致，排除数据本身造成的差异', () => {
    expect(makeTorrents(50, 123)).toEqual(makeTorrents(50, 123))
    expect(makeTorrents(50, 123)).not.toEqual(makeTorrents(50, 456))
  })

  it('数值落在合法域内，解析层不会因越界降级', () => {
    for (const t of makeTorrents(200)) {
      expect(t.progress).toBeGreaterThanOrEqual(0)
      expect(t.progress).toBeLessThanOrEqual(1)
      expect(t.name.length).toBeGreaterThan(0)
      expect(['downloading', 'seeding', 'paused', 'checking', 'queued']).toContain(t.state)
    }
  })
})
