import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import App from '@/App.vue'
import { router } from '@/router'

describe('App navigation', () => {
  it('renders Ant menu labels as links instead of function source', async () => {
    await router.push('/overview')
    await router.isReady()
    const wrapper = mount(App)
    await flushPromises()

    expect(wrapper.text()).toContain('回测复盘')
    expect(wrapper.text()).toContain('交易对管理')
    expect(wrapper.text()).not.toContain('=>')
  })
})
