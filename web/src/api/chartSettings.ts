import { api } from '@/api/client'
import type {
  ChartIndicatorLineSetting,
  ChartIndicatorSettings,
  ChartLineAppearance,
  ChartLineStyle,
  ChartLineWidth,
  ChartPriceLineSetting,
} from '@/api/types'
import { DEFAULT_CHART_INTERVAL, isChartInterval } from '@/shared/chartIntervals'

type DeepPartial<T> = {
  [Key in keyof T]?: T[Key] extends Array<infer Item>
    ? Array<DeepPartial<Item>>
    : T[Key] extends object
      ? DeepPartial<T[Key]>
      : T[Key]
}

type ChartIndicatorSettingsResponse = DeepPartial<ChartIndicatorSettings> & {
  updated_at?: string | null
}

const LINE_STYLES = new Set<ChartLineStyle>(['solid', 'dashed', 'dotted'])
const LINE_WIDTHS = new Set<ChartLineWidth>([1, 2, 3, 4])

function lineAppearance(
  value: DeepPartial<ChartLineAppearance> | undefined,
  fallbackStyle: ChartLineStyle = 'solid',
): ChartLineAppearance {
  const style = value?.style && LINE_STYLES.has(value.style) ? value.style : fallbackStyle
  const width = value?.width && LINE_WIDTHS.has(value.width) ? value.width : 1
  return { style, width }
}

function indicatorLine(value: DeepPartial<ChartIndicatorLineSetting>): ChartIndicatorLineSetting {
  return {
    period: value.period!,
    color: value.color!,
    ...lineAppearance(value),
  }
}

function priceLine(
  value: DeepPartial<ChartPriceLineSetting> | undefined,
  fallbackStyle: ChartLineStyle,
): ChartPriceLineSetting {
  return {
    visible: typeof value?.visible === 'boolean' ? value.visible : true,
    ...lineAppearance(value, fallbackStyle),
  }
}

function settingsDocument(response: ChartIndicatorSettingsResponse): ChartIndicatorSettings {
  const { updated_at: _updatedAt, default_interval, display, main, sub } = response
  return {
    display: {
      default_bar_spacing: display?.default_bar_spacing ?? 8,
      price_lines: {
        signal: priceLine(display?.price_lines?.signal, 'dashed'),
        average: priceLine(display?.price_lines?.average, 'solid'),
        invalid: priceLine(display?.price_lines?.invalid, 'dotted'),
        extensions: priceLine(display?.price_lines?.extensions, 'dashed'),
      },
    },
    main: {
      ema: {
        enabled: main!.ema!.enabled!,
        lines: main!.ema!.lines!.map(indicatorLine),
      },
      ma: {
        enabled: main!.ma!.enabled!,
        lines: main!.ma!.lines!.map(indicatorLine),
      },
      boll: {
        enabled: main!.boll!.enabled!,
        period: main!.boll!.period!,
        deviation: main!.boll!.deviation!,
        colors: main!.boll!.colors as ChartIndicatorSettings['main']['boll']['colors'],
        lines: {
          boundary: lineAppearance(main!.boll!.lines?.boundary),
          middle: lineAppearance(main!.boll!.lines?.middle, 'dashed'),
        },
      },
    },
    sub: {
      volume: {
        enabled: sub!.volume!.enabled!,
        ma_lines: sub!.volume!.ma_lines!.map(indicatorLine),
      },
      macd: {
        enabled: sub!.macd!.enabled!,
        fast_period: sub!.macd!.fast_period!,
        slow_period: sub!.macd!.slow_period!,
        signal_period: sub!.macd!.signal_period!,
        colors: sub!.macd!.colors as ChartIndicatorSettings['sub']['macd']['colors'],
        lines: {
          dif: lineAppearance(sub!.macd!.lines?.dif),
          dea: lineAppearance(sub!.macd!.lines?.dea),
        },
      },
      kdj: {
        enabled: sub!.kdj!.enabled!,
        period: sub!.kdj!.period!,
        colors: sub!.kdj!.colors as ChartIndicatorSettings['sub']['kdj']['colors'],
        lines: {
          k: lineAppearance(sub!.kdj!.lines?.k),
          d: lineAppearance(sub!.kdj!.lines?.d),
          j: lineAppearance(sub!.kdj!.lines?.j),
        },
      },
      rsi: {
        enabled: sub!.rsi!.enabled!,
        lines: sub!.rsi!.lines!.map(indicatorLine),
      },
      atr: {
        enabled: sub!.atr!.enabled!,
        period: sub!.atr!.period!,
        color: sub!.atr!.color!,
        line: lineAppearance(sub!.atr!.line),
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
