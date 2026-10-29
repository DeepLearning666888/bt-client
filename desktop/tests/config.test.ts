// @vitest-environment jsdom
/** AC-03：前端优先读壳注入的端点，注入缺失时回退构建期环境变量。
 *
 * 为什么要测这个顺序：壳注入失败时如果静默回退到构建期变量，界面会连到一个
 * 完全不相干的端口（那是开发者本机的引擎），表现为「能连上但数据不是你的」。
 * 反过来，注入存在却不优先，等于壳白改了。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readEngineConfig, portOf } from '../src/config'
import { notifyShellReady, resetShellBridge } from '../src/shell-bridge'

function injectEngine(value: unknown): void {
  ;(window as unknown as { __ENGINE__?: unknown }).__ENGINE__ = value
}

beforeEach(() => {
  injectEngine(undefined)
  vi.unstubAllEnvs()
})

afterEach(() => {
  injectEngine(undefined)
  vi.unstubAllEnvs()
  resetShellBridge()
})

describe('readEngineConfig 来源优先级', () => {
  it('注入存在时直接用它', () => {
    injectEngine({ baseUrl: 'http://127.0.0.1:5000', token: 'shell-token' })
    const config = readEngineConfig()
    expect(config).toEqual({ baseUrl: 'http://127.0.0.1:5000', token: 'shell-token' })
  })

  it('注入优先于构建期环境变量', () => {
    vi.stubEnv('VITE_ENGINE_URL', 'http://127.0.0.1:1111')
    vi.stubEnv('VITE_ENGINE_TOKEN', 'env-token')
    injectEngine({ baseUrl: 'http://127.0.0.1:2222', token: 'shell-token' })

    const config = readEngineConfig()
    expect(config?.baseUrl).toBe('http://127.0.0.1:2222')
    expect(config?.token).toBe('shell-token')
  })

  it('去掉 baseUrl 尾部斜杠，避免拼出双斜杠路径', () => {
    injectEngine({ baseUrl: 'http://127.0.0.1:5000///', token: 't' })
    expect(readEngineConfig()?.baseUrl).toBe('http://127.0.0.1:5000')
  })

  it('注入字段类型不对时回退到环境变量', () => {
    vi.stubEnv('VITE_ENGINE_URL', 'http://127.0.0.1:1111')
    vi.stubEnv('VITE_ENGINE_TOKEN', 'env-token')
    injectEngine({ baseUrl: 123, token: null })

    const config = readEngineConfig()
    expect(config?.baseUrl).toBe('http://127.0.0.1:1111')
    expect(config?.token).toBe('env-token')
  })

  it('注入是空串时回退到环境变量', () => {
    vi.stubEnv('VITE_ENGINE_URL', 'http://127.0.0.1:1111')
    vi.stubEnv('VITE_ENGINE_TOKEN', 'env-token')
    injectEngine({ baseUrl: '   ', token: '' })

    expect(readEngineConfig()?.token).toBe('env-token')
  })

  it('两种来源都没有时返回 null', () => {
    expect(readEngineConfig()).toBeNull()
  })

  it('只有环境变量时走浏览器形态', () => {
    vi.stubEnv('VITE_ENGINE_URL', 'http://127.0.0.1:3333')
    vi.stubEnv('VITE_ENGINE_TOKEN', 'env-token')
    expect(readEngineConfig()).toEqual({
      baseUrl: 'http://127.0.0.1:3333',
      token: 'env-token',
    })
  })
})

describe('portOf', () => {
  it('取出端口', () => {
    expect(portOf({ baseUrl: 'http://127.0.0.1:45678', token: 't' })).toBe(45678)
  })

  it('非法地址返回 0 而不是抛异常', () => {
    expect(portOf({ baseUrl: 'not-a-url', token: 't' })).toBe(0)
  })

  it('没有端口时返回 0', () => {
    expect(portOf({ baseUrl: 'http://127.0.0.1', token: 't' })).toBe(0)
  })
})

describe('notifyShellReady', () => {
  it('浏览器环境（没有 __TAURI__）是空操作，不抛异常', () => {
    expect(() => notifyShellReady(1234, 1)).not.toThrow()
  })

  it('有 __TAURI__ 时调用一次并带 port/seq', async () => {
    const calls: Array<{ command: string; args?: Record<string, unknown> }> = []
    ;(window as unknown as { __TAURI__?: unknown }).__TAURI__ = {
      core: {
        invoke: (command: string, args?: Record<string, unknown>) => {
          calls.push({ command, args })
          return Promise.resolve(null)
        },
      },
    }

    notifyShellReady(5000, 7)
    expect(calls).toHaveLength(1)
    expect(calls[0]?.command).toBe('frontend_ready')
    expect(calls[0]?.args).toEqual({ port: 5000, seq: 7 })

    // 只回报一次：10Hz 轮询下每帧都调会刷爆日志
    notifyShellReady(5000, 8)
    expect(calls).toHaveLength(1)

    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__
  })
})
