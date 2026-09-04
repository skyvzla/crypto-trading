<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { message } from 'ant-design-vue'
import {
  ArrowDownToLine,
  ArrowUpToLine,
  Database,
  Globe2,
  Maximize2,
  Minimize2,
  RefreshCw,
  RotateCcw,
  Settings2,
  SlidersHorizontal,
} from 'lucide-vue-next'
import { backtestApi } from '@/api/backtests'
import { chartSettingsApi } from '@/api/chartSettings'
import type { BacktestCandle, ChartIndicatorSettings, ChartOverlay } from '@/api/types'
import ChartIndicatorSettingsModal from '@/features/backtests/ChartIndicatorSettingsModal.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'
import {
  CHART_INDICATORS,
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
  indicatorEnabled,
} from '@/features/backtests/chartIndicatorSettings'
import { getChartTheme } from '@/features/backtests/chartTheme'
import { CHART_INTERVALS, DEFAULT_CHART_INTERVAL, isChartInterval, type ChartInterval } from '@/shared/chartIntervals'
import { timestampMs } from '@/shared/time'
import { IS_DARK_THEME } from '@/shared/theme'
import type { TradeChartData, TradeChartFillTimeSemantics } from './tradeChart'

const intervalMs: Record<ChartInterval, number> = {
  '1s': 1_000,
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '6h': 21_600_000,
  '8h': 28_800_000,
  '12h': 43_200_000,
  '1d': 86_400_000,
}

const props = withDefaults(
  defineProps<{
    trade: TradeChartData
    mode?: 'backtest' | 'market'
    researchId?: string
    overlays?: ChartOverlay[]
    fillTimeSemantics?: TradeChartFillTimeSemantics
    exitLabel?: string
    strategyLines?: boolean
  }>(),
  {
    mode: 'backtest',
    fillTimeSemantics: 'backtest-confirmation',
    exitLabel: '退出成交',
    strategyLines: true,
  },
)

const isDarkTheme = inject(
  IS_DARK_THEME,
  computed(() => false),
)
const queryClient = useQueryClient()
const palette = computed(() => getChartTheme(isDarkTheme.value))

/**
 * 图例。颜色直接取画布调色板，而不是在 CSS 里另抄一份，
 * 否则改了 chartTheme，图例就会和实际标线不一致。
 */
const legendItems = computed(() => {
  const colors = palette.value
  return [
    { label: 'B 买入成交', color: colors.up, dashed: false, strong: true },
    { label: 'S 卖出成交', color: colors.down, dashed: false, strong: false },
    { label: '信号', color: colors.signal, dashed: true, strong: false },
    { label: '开仓均价', color: colors.average, dashed: false, strong: false },
    { label: '失效价', color: colors.invalid, dashed: true, strong: false },
  ]
})

const interval = ref<ChartInterval>(DEFAULT_CHART_INTERVAL)
const intervalSelectedByUser = ref(false)
const windowCenterMs = ref<number | null>(null)
const chartRef = ref<InstanceType<typeof TradeCandlestickChart> | null>(null)
const chartSection = ref<HTMLElement | null>(null)
const isFullscreen = ref(false)
const indicatorSettingsOpen = ref(false)
const indicatorSettingsSaving = ref(false)
const indicatorSettings = ref<ChartIndicatorSettings>(cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS))
const lineVisibility = ref({ signal: true, average: true, invalid: true, extensions: true })
const focusTimeMs = ref<number | null>(null)
const loadedCandles = ref<BacktestCandle[]>([])

const availableIntervals = computed<ChartInterval[]>(() =>
  props.mode === 'market' ? CHART_INTERVALS.filter((item) => item !== '1s') : [...CHART_INTERVALS],
)
const source = computed<'binance' | 'archive'>(() =>
  props.mode === 'market' || interval.value !== '1s' ? 'binance' : 'archive',
)
const sourceLabel = computed(() => (source.value === 'archive' ? '本地归档' : 'Binance'))
const isReady = computed(() =>
  Boolean(
    props.trade.symbol && timestampMs(props.trade.entry_time) !== null && (props.mode === 'market' || props.researchId),
  ),
)
function resolveDefaultInterval(preferred: string | undefined): ChartInterval {
  if (preferred && availableIntervals.value.includes(preferred as ChartInterval)) {
    return preferred as ChartInterval
  }
  return availableIntervals.value[0] ?? DEFAULT_CHART_INTERVAL
}

const indicatorSettingsQuery = useQuery({
  queryKey: ['chart-indicator-settings'],
  queryFn: chartSettingsApi.get,
  staleTime: Number.POSITIVE_INFINITY,
  retry: 1,
})
const settingsResolved = computed(
  () =>
    indicatorSettingsQuery.isSuccess.value ||
    (indicatorSettingsQuery.isError.value && !indicatorSettingsQuery.isFetching.value),
)
const chartLoadingLabel = computed(() => (settingsResolved.value ? `加载 ${sourceLabel.value} K线` : '加载图表设置'))
const candleParams = computed(() => {
  if (!isReady.value || !settingsResolved.value) return null
  const entry = timestampMs(props.trade.entry_time)
  if (entry === null) return null
  const focus = focusTimeMs.value ?? entry
  const halfWindowBars = 750
  const padding = intervalMs[interval.value] * halfWindowBars
  const windowCenter = Math.max(padding, windowCenterMs.value ?? focus)
  return {
    ...(props.mode === 'backtest' ? { research_id: props.researchId } : {}),
    symbol: props.trade.symbol,
    interval: interval.value,
    start_ms: windowCenter - padding,
    end_ms: windowCenter + padding,
    source: source.value,
  }
})
const candlesQuery = useQuery({
  queryKey: computed(() => [
    'trade-replay-candles',
    props.mode,
    props.researchId,
    props.trade.symbol,
    candleParams.value,
  ]),
  queryFn: () => backtestApi.candles(candleParams.value!),
  enabled: computed(() => candleParams.value !== null),
  staleTime: 5 * 60_000,
  placeholderData: (previous) => previous,
})
const activeIndicatorNames = computed(() =>
  CHART_INDICATORS.filter((definition) => indicatorEnabled(indicatorSettings.value, definition)).map(
    (definition) => definition.name,
  ),
)

function selectInterval(value: string) {
  if (!isChartInterval(value) || !availableIntervals.value.includes(value)) return
  intervalSelectedByUser.value = true
  interval.value = value
  windowCenterMs.value = null
}

function applyDefaultInterval(preferred: string | undefined) {
  if (intervalSelectedByUser.value) return
  const nextInterval = resolveDefaultInterval(preferred)
  if (interval.value !== nextInterval) {
    interval.value = nextInterval
    windowCenterMs.value = null
  }
}

async function focusTradeEvent(kind: 'entry' | 'exit') {
  const firstFillTime = props.trade.fills?.[0]?.time
  const target = timestampMs(kind === 'entry' ? (firstFillTime ?? props.trade.entry_time) : props.trade.exit_time)
  if (target === null) return
  focusTimeMs.value = target
  windowCenterMs.value = null
  await nextTick()
  if (kind === 'entry') chartRef.value?.focusEntry?.()
  else chartRef.value?.focusExit?.()
}

function requestMore(direction: 'before' | 'after') {
  const boundary =
    direction === 'before' ? loadedCandles.value[0]?.time : loadedCandles.value[loadedCandles.value.length - 1]?.time
  if (boundary === undefined) return
  windowCenterMs.value = boundary * 1_000
}

async function toggleFullscreen() {
  if (!chartSection.value) return
  if (document.fullscreenElement === chartSection.value) await document.exitFullscreen()
  else await chartSection.value.requestFullscreen()
}

function syncFullscreenState() {
  isFullscreen.value = document.fullscreenElement === chartSection.value
}

function resetChartSize() {
  chartRef.value?.resetSize()
}

function openIndicatorSettings() {
  if (!settingsResolved.value) return
  indicatorSettingsOpen.value = true
  if (indicatorSettingsQuery.isError.value) message.warning('指标设置读取失败，当前使用默认配置')
}

async function saveIndicatorSettings(settings: ChartIndicatorSettings) {
  const lineGroups = [
    settings.main.ema.lines,
    settings.main.ma.lines,
    settings.sub.volume.ma_lines,
    settings.sub.rsi.lines,
  ]
  if (lineGroups.some((lines) => new Set(lines.map((line) => line.period)).size !== lines.length)) {
    message.error('同一指标不能配置重复周期')
    return
  }
  if (settings.sub.macd.fast_period >= settings.sub.macd.slow_period) {
    message.error('MACD 快线周期必须小于慢线周期')
    return
  }
  indicatorSettingsSaving.value = true
  try {
    await queryClient.cancelQueries({ queryKey: ['chart-indicator-settings'] })
    const saved = await chartSettingsApi.update(settings)
    indicatorSettings.value = cloneChartIndicatorSettings(saved)
    queryClient.setQueryData(['chart-indicator-settings'], saved)
    applyDefaultInterval(saved.default_interval)
    indicatorSettingsOpen.value = false
    message.success('图表指标设置已保存')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '图表指标设置保存失败')
  } finally {
    indicatorSettingsSaving.value = false
  }
}

onMounted(() => document.addEventListener('fullscreenchange', syncFullscreenState))
onBeforeUnmount(() => document.removeEventListener('fullscreenchange', syncFullscreenState))

watch(
  availableIntervals,
  (items) => {
    if (!items.includes(interval.value)) interval.value = items[0] ?? DEFAULT_CHART_INTERVAL
  },
  { immediate: true },
)
watch(
  () => candlesQuery.data.value,
  (response) => {
    if (!response || response.interval !== interval.value || response.source !== source.value) return
    const byTime = new Map(loadedCandles.value.map((candle) => [candle.time, candle]))
    response.candles.forEach((candle) => byTime.set(candle.time, candle))
    loadedCandles.value = [...byTime.values()].sort((left, right) => left.time - right.time)
  },
  { immediate: true },
)
watch(
  () => indicatorSettingsQuery.data.value,
  (settings) => {
    if (settings) indicatorSettings.value = cloneChartIndicatorSettings(settings)
  },
  { immediate: true },
)
watch(
  [() => settingsResolved.value, () => indicatorSettingsQuery.data.value],
  ([resolved, settings]) => {
    if (!resolved) return
    applyDefaultInterval(settings?.default_interval ?? DEFAULT_CHART_INDICATOR_SETTINGS.default_interval)
  },
  { immediate: true },
)
watch(
  () => [props.trade.symbol, props.trade.entry_time, props.trade.exit_time, interval.value, source.value],
  () => {
    loadedCandles.value = []
    windowCenterMs.value = null
  },
)
watch(
  () => props.trade.entry_time,
  () => {
    focusTimeMs.value = null
  },
)
</script>

<template>
  <section ref="chartSection" class="chart-section">
    <div class="chart-toolbar">
      <a-radio-group
        :value="interval"
        size="small"
        @change="(event: { target: { value: string } }) => selectInterval(event.target.value)"
      >
        <a-radio-button v-for="item in availableIntervals" :key="item" :value="item">{{ item }}</a-radio-button>
      </a-radio-group>
      <div class="source-tools">
        <a-tooltip :title="mode === 'market' ? 'Binance 公开 K 线；买卖点来自账本成交' : undefined">
          <a-tag color="blue"
            ><Database v-if="source === 'archive'" :size="14" /> <Globe2 v-else :size="14" /> {{ sourceLabel }}</a-tag
          >
        </a-tooltip>
        <a-divider type="vertical" />
        <a-tooltip :title="`配置技术指标：${activeIndicatorNames.join('、') || '未启用'}`">
          <a-button
            type="text"
            class="chart-tool-button"
            aria-label="配置技术指标"
            :disabled="!settingsResolved"
            @click="openIndicatorSettings"
          >
            <template #icon>
              <Settings2 :size="16" />
            </template>
            指标
          </a-button>
        </a-tooltip>
        <a-dropdown :trigger="['click']">
          <a-tooltip title="标线显示"
            ><a-button type="text" shape="circle" class="chart-icon-button" aria-label="标线显示"
              ><template #icon><SlidersHorizontal :size="16" /></template></a-button
          ></a-tooltip>
          <template #overlay>
            <a-card size="small" :bordered="false" @click.stop>
              <a-space direction="vertical" :size="8">
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.signal">信号价</a-checkbox>
                <a-checkbox v-model:checked="lineVisibility.average">开仓均价</a-checkbox>
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.invalid">失效价</a-checkbox>
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.extensions">策略扩展价位</a-checkbox>
              </a-space>
            </a-card>
          </template>
        </a-dropdown>
        <a-divider type="vertical" />
        <a-tooltip title="跳转到第一笔成交"
          ><a-button
            type="text"
            class="chart-tool-button"
            aria-label="跳转到第一笔成交"
            @click="focusTradeEvent('entry')"
            ><template #icon><ArrowUpToLine :size="15" /></template>首笔成交</a-button
          ></a-tooltip
        >
        <a-tooltip :title="`跳转到${exitLabel}`"
          ><a-button
            type="text"
            class="chart-tool-button"
            :aria-label="`跳转到${exitLabel}`"
            @click="focusTradeEvent('exit')"
            ><template #icon><ArrowDownToLine :size="15" /></template>{{ exitLabel }}</a-button
          ></a-tooltip
        >
        <a-spin v-if="candlesQuery.isFetching.value" size="small" />
        <a-tooltip title="刷新 K 线"
          ><a-button
            type="text"
            shape="circle"
            class="chart-icon-button"
            aria-label="刷新K线"
            @click="candlesQuery.refetch()"
            ><template #icon><RefreshCw :size="16" /></template></a-button
        ></a-tooltip>
        <a-tooltip title="恢复默认尺寸"
          ><a-button
            type="text"
            shape="circle"
            class="chart-icon-button"
            aria-label="恢复默认尺寸"
            @click="resetChartSize"
            ><template #icon><RotateCcw :size="16" /></template></a-button
        ></a-tooltip>
        <a-tooltip :title="isFullscreen ? '退出全屏' : '全屏查看'"
          ><a-button
            type="text"
            shape="circle"
            class="chart-icon-button"
            :aria-label="isFullscreen ? '退出全屏' : '全屏查看'"
            @click="toggleFullscreen"
            ><template #icon
              ><Minimize2 v-if="isFullscreen" :size="16" /><Maximize2 v-else :size="16" /></template></a-button
        ></a-tooltip>
      </div>
    </div>
    <div
      v-if="(!settingsResolved || candlesQuery.isFetching.value) && loadedCandles.length === 0"
      class="chart-loading"
    >
      <a-spin />
      <span>{{ chartLoadingLabel }}</span>
    </div>
    <QueryPanel
      v-else
      :error="loadedCandles.length ? null : candlesQuery.error.value"
      :empty="loadedCandles.length === 0"
      @retry="candlesQuery.refetch()"
    >
      <TradeCandlestickChart
        ref="chartRef"
        :candles="loadedCandles"
        :trade="trade"
        :overlays="overlays"
        :indicator-settings="indicatorSettings"
        :line-visibility="lineVisibility"
        :focus-time="focusTimeMs"
        :fill-time-semantics="fillTimeSemantics"
        @request-more="requestMore"
      />
    </QueryPanel>
    <div class="chart-legend">
      <a-tag color="blue">{{ candlesQuery.data.value?.source === 'archive' ? '本地归档' : 'Binance' }}</a-tag>
      <span
        v-for="item in legendItems"
        :key="item.label"
        class="legend-item"
        :class="{ 'is-dashed': item.dashed, 'is-strong': item.strong }"
        :style="{ color: item.color }"
        ><i />{{ item.label }}</span
      >
    </div>
    <ChartIndicatorSettingsModal
      v-model:open="indicatorSettingsOpen"
      :settings="indicatorSettings"
      :saving="indicatorSettingsSaving"
      @save="saveIndicatorSettings"
    />
  </section>
</template>
