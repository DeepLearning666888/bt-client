/** 探针页的独立构建配置。
 *
 * 为什么不和主应用共用一个 config：主应用多入口会被 rollup 拆成共享 chunk，
 * 而内联成单文件时那些跨 chunk 的 `import` 语句无法解析（每个内联 script 都是
 * 独立模块作用域）。单入口 + inlineDynamicImports 才能保证只产出一个 JS。
 *
 * cssCodeSplit: false 同理 —— 只要一个 CSS 文件，好内联。
 */

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // file:// 下必须用相对路径，绝对 /perf.js 会解析到盘符根目录
  base: './',
  build: {
    target: 'es2022',
    outDir: 'dist-perf',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: 'perf.html',
      output: {
        // vite 8 用 codeSplitting 取代了 inlineDynamicImports（后者已弃用并告警）
        codeSplitting: false,
        entryFileNames: 'perf.js',
        assetFileNames: 'perf.[ext]',
      },
    },
  },
})
