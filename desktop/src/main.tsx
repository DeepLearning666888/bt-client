import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { readEngineConfig } from './config'
import { createEngineRuntime } from './runtime'
import './styles.css'

// 编排全在 runtime 里，这里只剩「建运行时 + 挂载」。
// 这样拆的理由见 runtime.ts 的头注释：挂在 main.tsx 里的东西没有测试能触达。
const runtime = createEngineRuntime(readEngineConfig())

const container = document.getElementById('root')
if (container === null) {
  throw new Error('找不到 #root 容器')
}

createRoot(container).render(
  <StrictMode>
    <App
      store={runtime.store}
      onAddTask={runtime.addTask}
      onAddFileTask={runtime.addFileTask}
      onRowActions={runtime.rowActions}
    />
  </StrictMode>,
)
