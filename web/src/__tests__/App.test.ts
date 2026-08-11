import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import App from '@/App.vue'
import { router } from '@/router'

describe('App navigation', () => {
  it('使用 Ant 顶部-侧边布局并正确渲染菜单链接', async () => {
    await router.push('/overview')
    await router.isReady()
    const wrapper = mount(App)
    await flushPromises()

    expect(wrapper.text()).toContain('回测复盘')
    expect(wrapper.text()).toContain('交易对管理')
    expect(wrapper.text()).not.toContain('=>')
    expect(wrapper.find('.app-header').exists()).toBe(true)
    expect(wrapper.find('.app-body').exists()).toBe(true)
    expect(wrapper.find('.header-menu.ant-menu-horizontal').exists()).toBe(true)
    expect(wrapper.find('.side-menu.ant-menu-inline').exists()).toBe(true)
  })
})
