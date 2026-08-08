import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PlaceholderView from '@/views/PlaceholderView.vue'
import { NEmpty } from 'naive-ui'

describe('PlaceholderView', () => {
  it('renders empty placeholder', () => {
    const wrapper = mount(PlaceholderView, {
      global: {
        mocks: {
          $route: { meta: { title: '总览' }, name: 'overview' }
        }
      }
    })
    expect(wrapper.findComponent(NEmpty).exists()).toBe(true)
  })
})
