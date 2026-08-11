import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import App from '@/App.vue'
import { router } from '@/router'

describe('App navigation', () => {
  it('使用 Ant 浅色侧栏与空白 Header，并正确渲染菜单链接', async () => {
    await router.push('/overview')
    await router.isReady()
    const wrapper = mount(App)
    await flushPromises()

    expect(wrapper.text()).toContain('回测复盘')
    expect(wrapper.text()).toContain('交易对管理')
    expect(wrapper.text()).not.toContain('=>')
    expect(wrapper.find('.app-header').exists()).toBe(true)
    expect(wrapper.find('.app-body').exists()).toBe(true)
    expect(wrapper.get('.app-header').text()).toBe('')
    expect(wrapper.find('.header-menu').exists()).toBe(false)
    expect(wrapper.find('.app-sider.ant-layout-sider-light').exists()).toBe(true)
    expect(wrapper.find('.side-menu.ant-menu-light').exists()).toBe(true)
    expect(wrapper.find('.side-menu.ant-menu-inline').exists()).toBe(true)
  })
})
