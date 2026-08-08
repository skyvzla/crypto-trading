import { config } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { router } from '@/router'

// 每个测试都能直接用 mount() 渲染 Naive UI / pinia / vue-router 组件。
config.global.plugins = [createPinia(), router]
