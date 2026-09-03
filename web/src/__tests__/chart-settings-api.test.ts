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

  it('整体更新全局指标设置', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settings.main.ma.enabled = true
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ...settings, updated_at: '2026-09-03T12:00:00Z' }))

    await expect(chartSettingsApi.update(settings)).resolves.toEqual(settings)
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/v1/chart-settings',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify(settings) }),
    )
  })
})
