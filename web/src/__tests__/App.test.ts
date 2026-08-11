import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import App from '@/App.vue'
import { router } from '@/router'

describe('App navigation', () => {
  it('使用深色侧栏、主题切换，并正确渲染菜单链接', async () => {
    await router.push('/overview')
    await router.isReady()
    const wrapper = mount(App)
    await flushPromises()

    expect(wrapper.text()).toContain('回测复盘')
    expect(wrapper.text()).toContain('交易对管理')
    expect(wrapper.text()).not.toContain('=>')
    expect(wrapper.find('.app-header').exists()).toBe(true)
    expect(wrapper.find('.app-body').exists()).toBe(true)
    expect(wrapper.find('button[aria-label="切换深色模式"]').exists()).toBe(true)
    await wrapper.get('button[aria-label="切换深色模式"]').trigger('click')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('trade-ledger-theme')).toBe('dark')
    expect(wrapper.find('button[aria-label="切换浅色模式"]').exists()).toBe(true)
    expect(wrapper.find('.app-sider.ant-layout-sider-dark').exists()).toBe(true)
    expect(wrapper.find('.side-menu.ant-menu-dark').exists()).toBe(true)
    expect(wrapper.find('.side-menu.ant-menu-inline').exists()).toBe(true)
  })
})
