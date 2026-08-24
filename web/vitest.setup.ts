import { config } from '@vue/test-utils'
import Antd from 'ant-design-vue'
import { createPinia } from 'pinia'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { beforeEach } from 'vitest'
import { router } from '@/router'
// 和 main.ts 一致地加载全局样式：配合 vite.config 的 test.css，
// 让 SCSS 编译错误在测试里就暴露，而不是等到打包。
import '@/styles/base.scss'

// Ant Design 的响应式组件依赖浏览器 matchMedia，jsdom 需要提供最小实现。
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false
  })
})

/**
 * 与 main.ts 一致地全局安装 vue-query：FilterBar 等共享组件用它做缓存，
 * 缺少 QueryClient 时挂载任何含这些组件的视图都会抛错。
 *
 * 关掉重试，并在每个用例前清空缓存，避免上一个用例的响应被下一个复用。
 */
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } }
})

beforeEach(() => {
  queryClient.clear()
})

config.global.plugins = [createPinia(), router, Antd, [VueQueryPlugin, { queryClient }]]
