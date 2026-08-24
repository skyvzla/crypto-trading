import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  base: '/',
  plugins: [
    vue(),
    // 按需解析模板里的 <a-*> 标签。
    //
    // 原先 main.ts 用 `.use(Antd)` 全量注册，104 个组件全部成为全局组件、
    // 全部被打进入口 chunk，而项目实际只用到 44 个——一半以上是永远不会
    // 渲染的组件代码。全量注册同时也让 Rollup 无法 tree-shake。
    //
    // importStyle: false —— ant-design-vue v4 用运行时 CSS-in-JS，
    // 不存在按组件引入的样式文件；reset.css 仍由 main.ts 显式引入。
    Components({
      dts: false,
      resolvers: [AntDesignVueResolver({ importStyle: false })]
    })
  ],
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
    css: true,
    setupFiles: ['./vitest.setup.ts']
  }
})
