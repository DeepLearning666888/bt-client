import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { readEngineConfig, stateUrl, authHeaders } from './config'
import { Poller, type SnapshotSource } from './poller'
import { EngineStore } from './store'
import './styles.css'

const store = new EngineStore()
const config = readEngineConfig()

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

  const poller = new Poller(
    source,
    (snapshot) => {
      store.applySnapshot(snapshot)
    },
    (error) => {
      store.markOffline(error instanceof Error ? error.message : String(error))
    },
  )
  poller.start()
  store.markConnecting()
}

const container = document.getElementById('root')
if (container === null) {
  throw new Error('找不到 #root 容器')
}

createRoot(container).render(
  <StrictMode>
    <App store={store} />
  </StrictMode>,
)
