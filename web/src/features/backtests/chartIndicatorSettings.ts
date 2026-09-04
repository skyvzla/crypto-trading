import type { ChartIndicatorSettings } from '@/api/types'
import { DEFAULT_CHART_INTERVAL } from '@/shared/chartIntervals'

export type ChartIndicatorKey = 'ema' | 'ma' | 'boll' | 'volume' | 'macd' | 'kdj' | 'rsi' | 'atr'
export type ChartIndicatorGroup = 'main' | 'sub'

export interface ChartIndicatorDefinition {
  key: ChartIndicatorKey
  group: ChartIndicatorGroup
  name: string
  description: string
}

export const CHART_INDICATORS: ChartIndicatorDefinition[] = [
  { key: 'ema', group: 'main', name: 'EMA', description: '指数移动平均线' },
  { key: 'ma', group: 'main', name: 'MA', description: '简单移动平均线' },
  { key: 'boll', group: 'main', name: 'BOLL', description: '布林带' },
  { key: 'volume', group: 'sub', name: 'VOL', description: '成交量与均量线' },
  { key: 'macd', group: 'sub', name: 'MACD', description: '指数平滑异同移动平均线' },
  { key: 'kdj', group: 'sub', name: 'KDJ', description: '随机指标' },
  { key: 'rsi', group: 'sub', name: 'RSI', description: '相对强弱指标' },
  { key: 'atr', group: 'sub', name: 'ATR', description: '真实波幅均值' },
]

export const DEFAULT_CHART_INDICATOR_SETTINGS: ChartIndicatorSettings = {
  default_interval: DEFAULT_CHART_INTERVAL,
  main: {
    ema: {
      enabled: false,
      lines: [
        { period: 9, color: '#f5c451' },
        { period: 21, color: '#66b3ff' },
      ],
    },
    ma: {
      enabled: false,
      lines: [
        { period: 5, color: '#f59e0b' },
        { period: 10, color: '#22c55e' },
        { period: 20, color: '#3b82f6' },
      ],
    },
    boll: {
      enabled: false,
      period: 20,
      deviation: 2,
      colors: {
        upper: '#ef4444',
        middle: '#eab308',
        lower: '#22c55e',
      },
    },
  },
  sub: {
    volume: {
      enabled: true,
      ma_lines: [
        { period: 5, color: '#f5c451' },
        { period: 20, color: '#4da3ff' },
      ],
    },
    macd: {
      enabled: false,
      fast_period: 12,
      slow_period: 26,
      signal_period: 9,
      colors: {
        dif: '#4da3ff',
        dea: '#f5c451',
        histogram_up: '#2ebd85',
        histogram_down: '#f05252',
      },
    },
    kdj: {
      enabled: false,
      period: 9,
      colors: {
        k: '#4da3ff',
        d: '#f5c451',
        j: '#d98bff',
      },
    },
    rsi: {
      enabled: false,
      lines: [
        { period: 6, color: '#f5c451' },
        { period: 12, color: '#4da3ff' },
        { period: 24, color: '#d98bff' },
      ],
    },
    atr: {
      enabled: false,
      period: 14,
      color: '#14b8a6',
    },
  },
}

export function cloneChartIndicatorSettings(settings: ChartIndicatorSettings): ChartIndicatorSettings {
  return JSON.parse(JSON.stringify(settings)) as ChartIndicatorSettings
}

export function indicatorEnabled(settings: ChartIndicatorSettings, definition: ChartIndicatorDefinition): boolean {
  if (definition.group === 'main') {
    return settings.main[definition.key as keyof ChartIndicatorSettings['main']].enabled
  }
  return settings.sub[definition.key as keyof ChartIndicatorSettings['sub']].enabled
}

export function setIndicatorEnabled(
  settings: ChartIndicatorSettings,
  definition: ChartIndicatorDefinition,
  enabled: boolean,
) {
  if (definition.group === 'main') {
    settings.main[definition.key as keyof ChartIndicatorSettings['main']].enabled = enabled
  } else {
    settings.sub[definition.key as keyof ChartIndicatorSettings['sub']].enabled = enabled
  }
}
