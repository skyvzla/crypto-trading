import { configDefaults, defineConfig } from 'vitest/config'
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
      resolvers: [AntDesignVueResolver({ importStyle: false })],
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 允许同一内网的其他设备访问复盘页面。
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // 开发时把 /api/v1 直接转给本地 ledger 服务，避免依赖 CORS 配置。
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 生产构建不需要 sourcemap；显式声明，避免被环境变量意外打开后把源码随产物发布。
    sourcemap: false,
    rollupOptions: {
      output: {
        // 首屏只依赖框架运行时，把框架和图表库拆成稳定的长缓存 chunk：
        // 业务代码每次发布都会变，框架不会，混在一起会让用户每次重新下载 90KB+。
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined
          }
          // `@vue/runtime-core`、`@vue/shared` 等子包必须和 `vue` 同组，
          // 否则拆分反而会把同一个运行时切成两份。
          if (/[\\/]node_modules[\\/](@vue|vue|vue-router|pinia|@tanstack)[\\/]/.test(id)) {
            return 'vendor'
          }
          if (/[\\/]node_modules[\\/]lightweight-charts[\\/]/.test(id)) {
            return 'charts'
          }
          return undefined
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    css: true,
    setupFiles: ['./vitest.setup.ts'],
    exclude: [...configDefaults.exclude, 'e2e/**'],
    testTimeout: 20_000,
    // 覆盖率闸门：目标是"锁住现状、防止下滑"，不是一次性冲高。
    //
    // 阈值要留出余量，否则会退化成噪音：实测 lines 77.96% / functions 65.17%，
    // 若贴着实测值设 77/65，functions 只差 0.17pp（1151/1766），漏掉 4 个函数就红，
    // 结果是有人把阈值调低而不是补测试。这里各留约 2pp 余量，仍能拦住真正的下滑
    // （一次无测试的大改动会明显拉低比例），但不会因个别文件波动误报。
    // 参考：stmt 76.3% / branch 65.26%，本次未纳入闸门。
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,vue}'],
      exclude: ['src/__tests__/**', 'src/**/*.d.ts', 'src/env.d.ts'],
      thresholds: { lines: 76, functions: 63 },
    },
  },
})
