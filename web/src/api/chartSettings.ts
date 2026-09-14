import { api } from '@/api/client'
import type {
  ChartIndicatorLineSetting,
  ChartIndicatorSettings,
  ChartLineAppearance,
  ChartLineStyle,
  ChartLineWidth,
  ChartPriceLineSetting,
} from '@/api/types'
import { DEFAULT_CHART_INDICATOR_SETTINGS } from '@/features/backtests/chartIndicatorSettings'
import { DEFAULT_CHART_INTERVAL, isChartInterval } from '@/shared/chartIntervals'

type ChartIndicatorSettingsResponse = ChartIndicatorSettings & {
  updated_at?: string | null
}

type RecordValue = Record<string, unknown>

function isRecord(value: unknown): value is RecordValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/**
 * The API returns a complete document, but older installations may still
 * return a partially migrated document. Fill only known settings fields from
 * the local defaults before applying the normalizers below.
 */
function withDefaults(source: unknown, defaults: unknown): unknown {
  if (Array.isArray(defaults)) {
    if (!Array.isArray(source)) return defaults
    const fallback = defaults[defaults.length - 1]
    return source.map((item, index) => withDefaults(item, defaults[index] ?? fallback))
  }
  if (isRecord(defaults)) {
    const sourceRecord = isRecord(source) ? source : {}
    return Object.fromEntries(
      Object.entries(defaults).map(([key, defaultValue]) => [key, withDefaults(sourceRecord[key], defaultValue)]),
    )
  }
  return source ?? defaults
}

const LINE_STYLES = new Set<ChartLineStyle>(['solid', 'dashed', 'dotted'])
const LINE_WIDTHS = new Set<ChartLineWidth>([1, 2, 3, 4])

function lineAppearance(value: ChartLineAppearance, fallbackStyle: ChartLineStyle = 'solid'): ChartLineAppearance {
  const style = LINE_STYLES.has(value.style) ? value.style : fallbackStyle
  const width = LINE_WIDTHS.has(value.width) ? value.width : 1
  return { style, width }
}

function indicatorLine(value: ChartIndicatorLineSetting): ChartIndicatorLineSetting {
  return {
    period: value.period,
    color: value.color,
    ...lineAppearance(value),
  }
}

function priceLine(value: ChartPriceLineSetting, fallbackStyle: ChartLineStyle): ChartPriceLineSetting {
  return {
    visible: value.visible,
    ...lineAppearance(value, fallbackStyle),
  }
}

function settingsDocument(response: ChartIndicatorSettingsResponse): ChartIndicatorSettings {
  const { default_interval, display, main, sub } = withDefaults(
    response,
    DEFAULT_CHART_INDICATOR_SETTINGS,
  ) as ChartIndicatorSettings
  return {
    display: {
      default_bar_spacing: display.default_bar_spacing,
      price_lines: {
        signal: priceLine(display.price_lines.signal, 'dashed'),
        average: priceLine(display.price_lines.average, 'solid'),
        invalid: priceLine(display.price_lines.invalid, 'dotted'),
        extensions: priceLine(display.price_lines.extensions, 'dashed'),
      },
    },
    main: {
      ema: {
        enabled: main.ema.enabled,
        lines: main.ema.lines.map(indicatorLine),
      },
      ma: {
        enabled: main.ma.enabled,
        lines: main.ma.lines.map(indicatorLine),
      },
      boll: {
        enabled: main.boll.enabled,
        period: main.boll.period,
        deviation: main.boll.deviation,
        colors: main.boll.colors,
        lines: {
          boundary: lineAppearance(main.boll.lines.boundary),
          middle: lineAppearance(main.boll.lines.middle, 'dashed'),
        },
      },
    },
    sub: {
      volume: {
        enabled: sub.volume.enabled,
        ma_lines: sub.volume.ma_lines.map(indicatorLine),
      },
      macd: {
        enabled: sub.macd.enabled,
        fast_period: sub.macd.fast_period,
        slow_period: sub.macd.slow_period,
        signal_period: sub.macd.signal_period,
        colors: sub.macd.colors,
        lines: {
          dif: lineAppearance(sub.macd.lines.dif),
          dea: lineAppearance(sub.macd.lines.dea),
        },
      },
      kdj: {
        enabled: sub.kdj.enabled,
        period: sub.kdj.period,
        colors: sub.kdj.colors,
        lines: {
          k: lineAppearance(sub.kdj.lines.k),
          d: lineAppearance(sub.kdj.lines.d),
          j: lineAppearance(sub.kdj.lines.j),
        },
      },
      rsi: {
        enabled: sub.rsi.enabled,
        lines: sub.rsi.lines.map(indicatorLine),
      },
      atr: {
        enabled: sub.atr.enabled,
        period: sub.atr.period,
        color: sub.atr.color,
        line: lineAppearance(sub.atr.line),
      },
    },
    default_interval: default_interval && isChartInterval(default_interval) ? default_interval : DEFAULT_CHART_INTERVAL,
  }
}

export const chartSettingsApi = {
  get: async () => settingsDocument(await api.get<ChartIndicatorSettingsResponse>('/chart-settings')),
  update: async (settings: ChartIndicatorSettings) =>
    settingsDocument(await api.put<ChartIndicatorSettingsResponse>('/chart-settings', settings)),
}
