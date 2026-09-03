import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ChartIndicatorSettingsModal from '@/features/backtests/ChartIndicatorSettingsModal.vue'
import {
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
} from '@/features/backtests/chartIndicatorSettings'

describe('ChartIndicatorSettingsModal', () => {
  it('按主副图列出指标，选择后显示对应参数且保存完整副本', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    const wrapper = mount(ChartIndicatorSettingsModal, {
      props: { open: true, settings },
      global: {
        stubs: {
          AModal: {
            props: ['title'],
            emits: ['ok'],
            template:
              '<div><h2>{{ title }}</h2><slot /><button class="save-probe" @click="$emit(\'ok\')">保存</button></div>',
          },
        },
      },
    })

    expect(wrapper.text()).toContain('主图指标')
    expect(wrapper.text()).toContain('副图指标')
    expect(wrapper.findAll('.indicator-list-item')).toHaveLength(8)

    const maItem = wrapper.findAll('.indicator-list-item').find((item) => item.text().includes('简单移动平均线'))!
    await maItem.trigger('click')
    expect(wrapper.get('section[aria-label="MA 参数"]').text()).toContain('增加周期')
    expect(wrapper.findAll('input[type="color"]')).toHaveLength(3)

    const maCheckbox = maItem.get('input[type="checkbox"]')
    await maCheckbox.setValue(true)
    await wrapper.get('.save-probe').trigger('click')

    const saved = wrapper.emitted('save')?.[0]?.[0] as typeof settings
    expect(saved.main.ma.enabled).toBe(true)
    expect(saved.main.ma.lines.map((line) => line.period)).toEqual([5, 10, 20])
    expect(saved).not.toBe(settings)
  })
})
