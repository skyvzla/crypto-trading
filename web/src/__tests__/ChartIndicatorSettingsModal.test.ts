import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ChartIndicatorSettingsModal from '@/features/backtests/ChartIndicatorSettingsModal.vue'
import {
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
} from '@/features/backtests/chartIndicatorSettings'

describe('ChartIndicatorSettingsModal', () => {
  it('用独立 Tabs 和 panels 隔离显示、主图与副图设置，并保存完整副本', async () => {
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
          ASelect: {
            props: ['value'],
            emits: ['update:value'],
            template: '<select :value="value" @change="$emit(\'update:value\', $event.target.value)"><slot /></select>',
          },
          ASelectOption: {
            props: ['value'],
            template: '<option :value="value"><slot /></option>',
          },
        },
      },
    })

    expect(wrapper.get('h2').text()).toBe('图表设置')
    expect(wrapper.findAll('[role="tab"]').map((tab) => tab.text())).toEqual(['显示', '主图指标', '副图指标'])
    expect(wrapper.get('.ant-tabs-tabpane-active [data-testid="display-tab-panel"]')).toBeTruthy()
    expect(wrapper.find('[data-testid="main-tab-panel"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="sub-tab-panel"]').exists()).toBe(false)
    expect(wrapper.get('section[aria-label="显示设置"]').text()).toContain('默认 K 线宽度')
    expect(wrapper.get('[data-testid="default-interval-row"]').classes()).toContain('display-setting-row')
    expect(wrapper.get('[data-testid="default-bar-spacing-row"]').classes()).toContain('display-setting-row')
    expect(wrapper.get('[data-testid="price-lines-panel"]').classes()).toContain('settings-panel')
    expect(wrapper.findAll('.price-line-row')).toHaveLength(4)

    await wrapper.get('select[id="default-chart-interval"]').setValue('15m')

    await wrapper.findAll('[role="tab"]')[1].trigger('click')
    expect(wrapper.get('.ant-tabs-tabpane-active [data-testid="main-tab-panel"]')).toBeTruthy()
    expect(wrapper.findAll('.ant-tabs-tabpane-active .indicator-list-item strong').map((item) => item.text())).toEqual([
      'EMA',
      'MA',
      'BOLL',
    ])
    expect(wrapper.find('.indicator-list > h3').exists()).toBe(false)

    const maItem = wrapper
      .findAll('.ant-tabs-tabpane-active .indicator-list-item')
      .find((item) => item.text().includes('简单移动平均线'))!
    await maItem.trigger('click')
    expect(wrapper.get('section[aria-label="MA 参数"]').text()).toContain('增加周期')
    expect(wrapper.findAll('input[type="color"]')).toHaveLength(3)
    expect((wrapper.get('select[aria-label="第 1 条线线型"]').element as HTMLSelectElement).value).toBe('solid')
    await wrapper.get('select[aria-label="第 1 条线线型"]').setValue('dotted')

    const maCheckbox = maItem.get('input[type="checkbox"]')
    await maCheckbox.setValue(true)

    const bollItem = wrapper
      .findAll('.ant-tabs-tabpane-active .indicator-list-item')
      .find((item) => item.text().includes('布林通道'))!
    await bollItem.trigger('click')
    const bollEditor = wrapper.get('section[aria-label="BOLL 参数"]')
    expect(bollEditor.text()).toContain('通道边界')
    expect(bollEditor.text()).toContain('中轨')
    expect(bollEditor.text()).toContain('通道填充')

    await wrapper.findAll('[role="tab"]')[2].trigger('click')
    expect(wrapper.get('.ant-tabs-tabpane-active [data-testid="sub-tab-panel"]')).toBeTruthy()
    expect(wrapper.findAll('.ant-tabs-tabpane-active .indicator-list-item strong').map((item) => item.text())).toEqual([
      'VOL',
      'MACD',
      'KDJ',
      'RSI',
      'ATR',
    ])

    await wrapper.get('.save-probe').trigger('click')

    const saved = wrapper.emitted('save')?.[0]?.[0] as typeof settings
    expect(saved.main.ma.enabled).toBe(true)
    expect(saved.default_interval).toBe('15m')
    expect(saved.main.ma.lines.map((line) => line.period)).toEqual([5, 10, 20])
    expect(saved.main.ma.lines[0]).toMatchObject({ style: 'dotted', width: 1 })
    expect(saved.display.default_bar_spacing).toBe(8)
    expect(saved.display.price_lines.invalid).toMatchObject({ visible: true, style: 'dotted', width: 1 })
    expect(saved).not.toBe(settings)
  })
})
