import { describe, expect, it } from 'vitest'
import {
  CHART_INDICATORS,
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
  indicatorEnabled,
  setIndicatorEnabled,
} from '@/features/backtests/chartIndicatorSettings'

describe('图表指标配置', () => {
  it('明确区分主图和副图指标', () => {
    expect(CHART_INDICATORS.filter((item) => item.group === 'main').map((item) => item.key)).toEqual([
      'ema',
      'ma',
      'boll',
    ])
    expect(CHART_INDICATORS.filter((item) => item.group === 'sub').map((item) => item.key)).toEqual([
      'volume',
      'macd',
      'kdj',
      'rsi',
      'atr',
    ])
  })

  it('编辑副本不会污染服务端配置对象', () => {
    const copy = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    copy.default_interval = '15m'
    copy.main.ma.lines[0].period = 30
    copy.sub.volume.ma_lines.push({ period: 60, color: '#123456', style: 'dotted', width: 2 })

    expect(DEFAULT_CHART_INDICATOR_SETTINGS.default_interval).toBe('1s')
    expect(DEFAULT_CHART_INDICATOR_SETTINGS.main.ma.lines[0].period).toBe(5)
    expect(DEFAULT_CHART_INDICATOR_SETTINGS.sub.volume.ma_lines).toHaveLength(2)
  })

  it('用同一入口读写主副图显隐状态', () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    const ma = CHART_INDICATORS.find((item) => item.key === 'ma')!
    const rsi = CHART_INDICATORS.find((item) => item.key === 'rsi')!

    setIndicatorEnabled(settings, ma, true)
    setIndicatorEnabled(settings, rsi, true)

    expect(indicatorEnabled(settings, ma)).toBe(true)
    expect(indicatorEnabled(settings, rsi)).toBe(true)
  })
})
