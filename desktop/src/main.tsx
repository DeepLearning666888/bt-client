import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { addTorrent } from './add-task'
import { readEngineConfig, stateUrl, authHeaders, portOf } from './config'
import { Poller, type SnapshotSource } from './poller'
import { notifyShellReady } from './shell-bridge'
import { EngineStore } from './store'
import './styles.css'

const store = new EngineStore()
const config = readEngineConfig()

/** 添加动作。没配置引擎时保持 undefined，界面据此不显示添加入口。 */
let addTask: ((magnet: string) => Promise<void>) | undefined

if (config === null) {
  store.markOffline('未配置引擎地址（VITE_ENGINE_URL / VITE_ENGINE_TOKEN）')
} else {
  const source: SnapshotSource = async (since) => {
    const response = await fetch(stateUrl(config, since), {
      headers: authHeaders(config),
    })
    if (!response.ok) {
      throw new Error(`/state 返回 ${response.status}`)
    }
    return (await response.json()) as unknown
  }

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
  )
  poller.start()
  store.markConnecting()

  addTask = async (magnet) => {
    await addTorrent(config, magnet)
    // 不等下一拍：用户刚点完添加，列表里 100ms 后才出现会像「没生效」。
    void poller.tick()
  }
}

const container = document.getElementById('root')
if (container === null) {
  throw new Error('找不到 #root 容器')
}

createRoot(container).render(
  <StrictMode>
    <App store={store} onAddTask={addTask} />
  </StrictMode>,
)
