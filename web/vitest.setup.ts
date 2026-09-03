import { config } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach } from 'vitest'
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
    dispatchEvent: () => false,
  }),
})

/**
 * 组件解析失败要让测试红掉。
 *
 * antd 组件改为按需解析（unplugin-vue-components）后，漏掉一个 `<a-*>` 既不会
 * 让打包失败也不会让 vue-tsc 报错，运行时只是一条 warning——页面上少一块 UI。
 * 这里把它升级成断言，是这套按需引入唯一的自动化防线。
 *
 * 也正因为如此，测试里刻意不再全局 `.use(Antd)`：那会把解析失败重新盖住。
 */
const unresolvedComponents: string[] = []
const originalWarn = console.warn
console.warn = (...args: unknown[]) => {
  const text = args.map((item) => String(item)).join(' ')
  if (text.includes('Failed to resolve component')) unresolvedComponents.push(text)
  originalWarn(...args)
}

/**
 * 与 main.ts 一致地全局安装 vue-query：FilterBar 等共享组件用它做缓存，
 * 缺少 QueryClient 时挂载任何含这些组件的视图都会抛错。
 *
 * 关掉重试，并在每个用例前清空缓存，避免上一个用例的响应被下一个复用。
 */
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
})

beforeEach(() => {
  queryClient.clear()
  unresolvedComponents.length = 0
})

afterEach(() => {
  if (!unresolvedComponents.length) return
  const messages = [...new Set(unresolvedComponents)]
  unresolvedComponents.length = 0
  throw new Error(`模板里有组件没有被解析出来：\n${messages.join('\n')}`)
})

config.global.plugins = [createPinia(), router, [VueQueryPlugin, { queryClient }]]
