import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 产物由 FastAPI 挂载在 /ui 下，构建时必须写死同名 base，
// 否则 index.html 里的 asset 引用会退回根路径 404。
export default defineConfig({
  base: '/ui/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    // 允许同一内网的其他设备访问复盘页面。
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // 开发时把 /api/v1 直接转给本地 ledger 服务，避免依赖 CORS 配置。
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts']
  }
})
