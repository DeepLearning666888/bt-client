/** 引擎运行时：把「配置」变成「可直接喂给 App 的一整套东西」。
 *
 * 从 main.tsx 抽出来的理由：这段编排（poller + store + 三个动作）是界面对引擎的
 * 全部依赖，但它原先只能通过「打开真正的窗口」被验证到。挂在 main.tsx 里的代码
 * 没有任何测试能触达 —— 而「引擎中途死掉界面怎么办」恰恰只能在这一层观察。
 *
 * 抽出来之后，main.tsx 只剩 render，测试可以照同一条路径搭出真实的运行时。
 */

import { addTorrent, addTorrentFile, type TorrentBytes } from './add-task'
import { authHeaders, portOf, stateUrl, type EngineConfig } from './config'
import { Poller, STATE_TIMEOUT_MS, type SnapshotSource } from './poller'
import { notifyShellReady } from './shell-bridge'
import { EngineStore } from './store'
import { pauseTorrent, removeTorrent, resumeTorrent } from './torrent-action'
import type { TorrentActions } from './components/TorrentList'

export interface EngineRuntime {
  store: EngineStore
  /** 没有配置引擎时保持 undefined，界面据此不显示添加入口 */
  addTask?: (magnet: string) => Promise<void>
  addFileTask?: (bytes: TorrentBytes, name: string) => Promise<void>
  rowActions?: TorrentActions
  /** 没配置引擎时为 null */
  poller: Poller | null
  /** 停掉定时器。生产里进程退出即结束，只有测试需要显式收尾。 */
  dispose(): void
}

export interface RuntimeOptions {
  /** 覆盖默认的 HTTP 数据源。
   *
   * 默认走真 fetch（live 测试与生产都走这条）。注入点存在的唯一理由是
   * 「数据源失效时界面怎么显示」这类规则需要可控的失效时点 —— 靠杀进程
   * 触发的话，断言的时机永远在跟操作系统赛跑。
   */
  source?: SnapshotSource
  /** 单次轮询的超时。默认 `STATE_TIMEOUT_MS`；测试调小以免每条都等 3 秒。 */
  timeoutMs?: number
  /** 是否立刻开始定时轮询。默认 true；需要手动控制每一拍时关掉（否则定时器会
   *  先占住 inflight，让手动 tick 被静默跳过）。 */
  startPolling?: boolean
}

export function createEngineRuntime(
  config: EngineConfig | null,
  options: RuntimeOptions = {},
): EngineRuntime {
  const store = new EngineStore()

  if (config === null) {
    store.markOffline('未配置引擎地址（VITE_ENGINE_URL / VITE_ENGINE_TOKEN）')
    return {
      store,
      poller: null,
      dispose: () => {},
    }
  }

  const source: SnapshotSource =
    options.source ??
    (async (since, signal) => {
      const response = await fetch(stateUrl(config, since), {
        headers: authHeaders(config),
        signal,
      })
      if (!response.ok) {
        throw new Error(`/state 返回 ${response.status}`)
      }
      return (await response.json()) as unknown
    })

  // 首帧数据到达后回报一次给壳。放在这里而不是组件里：这一层才知道
  // 「快照是真的从引擎拿到的」，组件只知道自己拿到了一个对象。
  const enginePort = portOf(config)
  let shellReported = false

  const poller = new Poller(
    source,
    (snapshot) => {
      store.applySnapshot(snapshot)
      if (!shellReported) {
        shellReported = true
        notifyShellReady(enginePort, snapshot.seq)
      }
    },
    (error) => {
      store.markOffline(error instanceof Error ? error.message : String(error))
    },
    100,
    options.timeoutMs ?? STATE_TIMEOUT_MS,
  )
  if (options.startPolling !== false) poller.start()
  store.markConnecting()

  const addTask = async (magnet: string): Promise<void> => {
    await addTorrent(config, magnet)
    // 不等下一拍：用户刚点完添加，列表里 100ms 后才出现会像「没生效」。
    void poller.tick()
  }

  const addFileTask = async (bytes: TorrentBytes): Promise<void> => {
    await addTorrentFile(config, bytes)
    void poller.tick()
  }

  // 三个操作都立刻 tick：等下一个 100ms 周期的话，用户会以为没生效再点一次，
  // 第二次就撞上「任务已经不在了」。
  const rowActions: TorrentActions = {
    pause: async (infohash) => {
      await pauseTorrent(config, infohash)
      void poller.tick()
    },
    resume: async (infohash) => {
      await resumeTorrent(config, infohash)
      void poller.tick()
    },
    remove: async (infohash, deleteFiles) => {
      await removeTorrent(config, infohash, deleteFiles)
      void poller.tick()
    },
  }

  return {
    store,
    addTask,
    addFileTask,
    rowActions,
    poller,
    dispose: () => {
      poller.stop()
    },
  }
}
