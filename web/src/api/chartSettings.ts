import { api } from '@/api/client'
import type { ChartIndicatorSettings } from '@/api/types'
import { DEFAULT_CHART_INTERVAL, isChartInterval } from '@/shared/chartIntervals'

type ChartIndicatorSettingsResponse = Omit<ChartIndicatorSettings, 'default_interval'> & {
  default_interval?: ChartIndicatorSettings['default_interval']
  updated_at?: string | null
}

function settingsDocument(response: ChartIndicatorSettingsResponse): ChartIndicatorSettings {
  const { updated_at: _updatedAt, ...settings } = response
  return {
    ...settings,
    default_interval:
      settings.default_interval && isChartInterval(settings.default_interval)
        ? settings.default_interval
        : DEFAULT_CHART_INTERVAL,
  }
}

export const chartSettingsApi = {
  get: async () => settingsDocument(await api.get<ChartIndicatorSettingsResponse>('/chart-settings')),
  update: async (settings: ChartIndicatorSettings) =>
    settingsDocument(await api.put<ChartIndicatorSettingsResponse>('/chart-settings', settings)),
}
