import { beforeEach, describe, expect, it, vi } from 'vitest'
import { chartSettingsApi } from '@/api/chartSettings'
import { jsonResponse } from './httpMocks'
import {
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
} from '@/features/backtests/chartIndicatorSettings'

describe('图表设置 API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('读取时移除服务端元数据，避免后续 PUT 带回只读字段', async () => {
    const response = {
      ...cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS),
      updated_at: '2026-09-03T12:00:00Z',
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(response))

    const settings = await chartSettingsApi.get()

    expect(settings).toEqual(DEFAULT_CHART_INDICATOR_SETTINGS)
    expect(settings).not.toHaveProperty('updated_at')
  })

  it('读取旧版响应时补充显示、线型、粗细和默认周期', async () => {
    const response = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    const legacyResponse = response as Partial<typeof DEFAULT_CHART_INDICATOR_SETTINGS>
    delete legacyResponse.default_interval
    delete legacyResponse.display
    const emaLine = response.main.ema.lines[0]!
    delete (emaLine as Partial<typeof emaLine>).style
    delete (emaLine as Partial<typeof emaLine>).width
    delete (response.main.boll as Partial<typeof response.main.boll>).lines
    delete (response.sub.atr as Partial<typeof response.sub.atr>).line
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(legacyResponse))

    const settings = await chartSettingsApi.get()

    expect(settings.default_interval).toBe('1s')
    expect(settings.display.default_bar_spacing).toBe(8)
    expect(settings.display.price_lines.invalid).toEqual({ visible: true, style: 'dotted', width: 1 })
    expect(settings.main.ema.lines[0]).toMatchObject({ style: 'solid', width: 1 })
    expect(settings.main.boll.lines.middle).toEqual({ style: 'dashed', width: 1 })
    expect(settings.sub.atr.line).toEqual({ style: 'solid', width: 1 })
  })

  it('整体更新全局指标设置', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settings.default_interval = '15m'
    settings.main.ma.enabled = true
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ...settings, updated_at: '2026-09-03T12:00:00Z' }))

    await expect(chartSettingsApi.update(settings)).resolves.toEqual(settings)
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/v1/chart-settings',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify(settings) }),
    )
  })
})
