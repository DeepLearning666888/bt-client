import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // 只服务本地桌面壳，不需要 legacy 兼容
    target: 'es2022',
    outDir: 'dist',
  },
  test: {
    // 默认 node 环境（数据层测试不需要 DOM）；需要 DOM 的用例在文件顶部写
    // @vitest-environment jsdom
    environment: 'node',
    include: ['tests/**/*.test.{ts,tsx}'],
    testTimeout: 30_000,
  },
})
