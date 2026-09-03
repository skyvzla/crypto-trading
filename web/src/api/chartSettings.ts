import { api } from '@/api/client'
import type { ChartIndicatorSettings } from '@/api/types'

type ChartIndicatorSettingsResponse = ChartIndicatorSettings & { updated_at?: string | null }

function settingsDocument(response: ChartIndicatorSettingsResponse): ChartIndicatorSettings {
  return { main: response.main, sub: response.sub }
}

export const chartSettingsApi = {
  get: async () => settingsDocument(await api.get<ChartIndicatorSettingsResponse>('/chart-settings')),
  update: async (settings: ChartIndicatorSettings) =>
    settingsDocument(await api.put<ChartIndicatorSettingsResponse>('/chart-settings', settings)),
}
