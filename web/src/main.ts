import { createApp } from 'vue'
import { createPinia } from 'pinia'
// 组件本身由 unplugin-vue-components 按需解析（见 vite.config.ts），
// 这里只保留全局样式重置。
import 'ant-design-vue/dist/reset.css'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import App from '@/App.vue'
import { router } from '@/router'
import '@/styles/base.scss'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false
    }
  }
})

createApp(App)
  .use(createPinia())
  .use(VueQueryPlugin, { queryClient })
  .use(router)
  .mount('#app')
