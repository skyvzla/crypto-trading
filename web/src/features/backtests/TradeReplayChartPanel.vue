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
} from 'lucide-vue-next'
import { backtestApi } from '@/api/backtests'
import { chartSettingsApi } from '@/api/chartSettings'
import { ApiError } from '@/api/client'
import { operationsApi } from '@/api/operations'
import type {
  BacktestCandle,
  CandleCoverageStatus,
  CampaignCandleSnapshotResponse,
  ChartIndicatorSettings,
  ChartOverlay,
} from '@/api/types'
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
    campaignId?: string
    accountId?: string
    strategyId?: string
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
  const priceLines = indicatorSettings.value.display.price_lines
  return [
    { label: 'B 买入成交', color: colors.up, style: 'solid', width: 3, strong: true },
    { label: 'S 卖出成交', color: colors.down, style: 'solid', width: 2, strong: false },
    ...(priceLines.signal.visible
      ? [{ label: '信号', color: colors.signal, ...priceLines.signal, strong: false }]
      : []),
    ...(priceLines.average.visible
      ? [{ label: '开仓均价', color: colors.average, ...priceLines.average, strong: false }]
      : []),
    ...(priceLines.invalid.visible
      ? [{ label: '失效价', color: colors.invalid, ...priceLines.invalid, strong: false }]
      : []),
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
const focusTimeMs = ref<number | null>(null)
const loadedCandles = ref<BacktestCandle[]>([])

interface CandleQueryResponse {
  symbol: string
  interval: string
  source: 'binance' | 'archive' | 'campaign_snapshot'
  candles: BacktestCandle[]
  coverage_status?: CandleCoverageStatus
  coverage_message?: string | null
  gap_count?: number
  expected_count?: number | null
  received_count?: number | null
}

type SnapshotUiStatus = CandleCoverageStatus | 'failed' | 'missing' | 'integrity' | 'error' | 'stale'

interface SnapshotErrorInfo {
  status: Extract<SnapshotUiStatus, 'collecting' | 'failed' | 'missing' | 'integrity' | 'error'>
  message: string
}

const hasCampaignSnapshotContext = computed(() =>
  Boolean(props.mode === 'market' && props.campaignId && props.accountId && props.strategyId),
)
const availableIntervals = computed<ChartInterval[]>(() =>
  props.mode === 'market' && !hasCampaignSnapshotContext.value
    ? CHART_INTERVALS.filter((item) => item !== '1s')
    : [...CHART_INTERVALS],
)
const source = computed<'binance' | 'archive' | 'campaign_snapshot'>(() =>
  props.mode === 'backtest' && interval.value === '1s'
    ? 'archive'
    : props.mode === 'market' && interval.value === '1s' && hasCampaignSnapshotContext.value
      ? 'campaign_snapshot'
      : 'binance',
)
const sourceLabel = computed(() => {
  if (source.value === 'archive') return '本地归档'
  if (source.value === 'campaign_snapshot') return '实盘快照'
  return 'Binance'
})
const isReady = computed(() =>
  Boolean(
    props.trade.symbol &&
    timestampMs(props.trade.entry_time) !== null &&
    (props.mode === 'backtest' ? props.researchId : true) &&
    (source.value !== 'campaign_snapshot' || hasCampaignSnapshotContext.value),
  ),
)

function finiteCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function snapshotCoverageStatus(response: CampaignCandleSnapshotResponse): CandleCoverageStatus {
  if (response.coverage_status) return response.coverage_status
  if (response.snapshot.gaps.length > 0) return 'gapped'
  const expected = finiteCount(
    response.expected_count ?? response.snapshot.coverage.expected_count ?? response.snapshot.coverage.expected_rows,
  )
  const received = finiteCount(
    response.received_count ?? response.snapshot.coverage.received_count ?? response.snapshot.coverage.present_rows,
  )
  if (expected !== null && received !== null && received < expected) return 'incomplete'
  return response.snapshot.status === 'completed' ? 'complete' : 'collecting'
}

function normalizeSnapshotResponse(response: CampaignCandleSnapshotResponse): CandleQueryResponse {
  const gaps = response.snapshot.gaps
  const coverage = response.snapshot.coverage
  return {
    symbol: response.symbol,
    interval: response.interval,
    source: response.source,
    candles: response.candles,
    coverage_status: snapshotCoverageStatus(response),
    coverage_message: response.coverage_message ?? response.snapshot.failure_reason,
    gap_count: response.gap_count ?? gaps.length,
    expected_count: response.expected_count ?? finiteCount(coverage.expected_count ?? coverage.expected_rows) ?? null,
    received_count:
      response.received_count ??
      response.snapshot.row_count ??
      finiteCount(coverage.received_count ?? coverage.present_rows),
  }
}
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
const candlesQuery = useQuery<CandleQueryResponse>({
  queryKey: computed(() => [
    'trade-replay-candles',
    props.mode,
    props.researchId,
    props.campaignId,
    props.accountId,
    props.strategyId,
    props.trade.symbol,
    candleParams.value,
  ]),
  queryFn: async () => {
    const params = candleParams.value!
    if (source.value === 'campaign_snapshot') {
      const response = await operationsApi.campaignSnapshot(props.campaignId!, {
        account_id: props.accountId!,
        strategy_id: props.strategyId!,
        symbol: params.symbol,
        interval: '1s',
        start_ms: params.start_ms,
        end_ms: params.end_ms,
      })
      return normalizeSnapshotResponse(response)
    }
    return await backtestApi.candles(params as Parameters<typeof backtestApi.candles>[0])
  },
  enabled: computed(() => candleParams.value !== null),
  staleTime: 5 * 60_000,
  placeholderData: (previous) => previous,
})

function snapshotErrorInfo(error: unknown): SnapshotErrorInfo {
  const detail = error instanceof Error ? error.message : ''
  const normalized = detail.toLowerCase()
  if (error instanceof ApiError && error.status === 404) {
    return { status: 'missing', message: '没有找到该 Campaign 的 1s 快照' }
  }
  if (
    (error instanceof ApiError && error.status === 503) ||
    /hash|sha256|checksum|integrity|完整性|校验|payload.*missing|missing.*payload/.test(normalized)
  ) {
    return { status: 'integrity', message: '1s 快照完整性校验失败或数据文件不可用' }
  }
  if (error instanceof ApiError && error.status === 409) {
    if (/not ready|collecting|采集|未就绪/.test(normalized)) {
      return { status: 'collecting', message: '1s 快照仍在采集，暂时不可读取' }
    }
    return { status: 'failed', message: detail || '1s 快照读取失败' }
  }
  return { status: 'error', message: detail || '1s 快照读取失败' }
}

const snapshotError = computed<SnapshotErrorInfo | null>(() => {
  if (source.value !== 'campaign_snapshot' || !candlesQuery.error.value) return null
  return snapshotErrorInfo(candlesQuery.error.value)
})
const snapshotStatus = computed<SnapshotUiStatus | null>(() => {
  if (source.value !== 'campaign_snapshot') return null
  if (snapshotError.value) return loadedCandles.value.length ? 'stale' : snapshotError.value.status
  return candlesQuery.data.value?.coverage_status ?? null
})
const snapshotStatusLabel = computed(() => {
  switch (snapshotStatus.value) {
    case 'complete':
      return '1s完整'
    case 'incomplete':
      return '1s不完整'
    case 'gapped':
      return `1s有缺口${candlesQuery.data.value?.gap_count ? ` · ${candlesQuery.data.value.gap_count}` : ''}`
    case 'collecting':
      return '1s采集中'
    case 'failed':
      return '1s失败'
    case 'missing':
      return '无1s快照'
    case 'integrity':
      return '1s完整性错误'
    case 'error':
      return '1s读取失败'
    case 'stale':
      return '1s过期 · 读取失败'
    default:
      return ''
  }
})
const snapshotStatusColor = computed(() => {
  switch (snapshotStatus.value) {
    case 'complete':
      return 'green'
    case 'gapped':
      return 'red'
    case 'incomplete':
      return 'orange'
    case 'missing':
      return 'orange'
    case 'failed':
    case 'integrity':
    case 'error':
    case 'stale':
      return 'red'
    default:
      return 'blue'
  }
})
const snapshotStatusMessage = computed(() => {
  if (snapshotError.value) return snapshotError.value.message
  return candlesQuery.data.value?.coverage_message || undefined
})
const candleQueryError = computed<Error | null>(() => {
  const error = candlesQuery.error.value
  if (!error) return null
  if (source.value === 'campaign_snapshot') return new Error(snapshotError.value?.message || '1s 快照读取失败')
  return error instanceof Error ? error : new Error('K线读取失败')
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
  () => [
    props.mode,
    props.researchId,
    props.campaignId,
    props.accountId,
    props.strategyId,
    props.trade.symbol,
    props.trade.entry_time,
    props.trade.exit_time,
    interval.value,
    source.value,
  ],
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
        <a-tooltip
          :title="
            mode === 'market'
              ? source === 'campaign_snapshot'
                ? '实盘 1s 快照；订单流来自 Market aggTrade 聚合'
                : 'Binance 公开 K 线；买卖点来自账本成交'
              : undefined
          "
        >
          <a-tag color="blue"
            ><Database v-if="source === 'archive'" :size="14" /> <Globe2 v-else :size="14" /> {{ sourceLabel }}</a-tag
          >
        </a-tooltip>
        <a-tag v-if="snapshotStatus" :color="snapshotStatusColor" :title="snapshotStatusMessage">
          {{ snapshotStatusLabel }}
        </a-tag>
        <a-divider type="vertical" />
        <a-tooltip :title="`图表设置；已启用指标：${activeIndicatorNames.join('、') || '无'}`">
          <a-button
            type="text"
            class="chart-tool-button"
            aria-label="图表设置"
            :disabled="!settingsResolved"
            @click="openIndicatorSettings"
          >
            <template #icon>
              <Settings2 :size="16" />
            </template>
            设置
          </a-button>
        </a-tooltip>
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
      :error="loadedCandles.length ? null : candleQueryError"
      :empty="loadedCandles.length === 0"
      @retry="candlesQuery.refetch()"
    >
      <TradeCandlestickChart
        ref="chartRef"
        :candles="loadedCandles"
        :trade="trade"
        :overlays="overlays"
        :indicator-settings="indicatorSettings"
        :focus-time="focusTimeMs"
        :fill-time-semantics="fillTimeSemantics"
        @request-more="requestMore"
      />
    </QueryPanel>
    <div class="chart-legend">
      <a-tag color="blue">{{ sourceLabel }}</a-tag>
      <span
        v-for="item in legendItems"
        :key="item.label"
        class="legend-item"
        :class="{
          'is-dashed': item.style === 'dashed',
          'is-dotted': item.style === 'dotted',
          'is-strong': item.strong,
        }"
        :style="{ color: item.color, '--legend-line-width': `${item.width}px` }"
        ><i />{{ item.label }}</span
      >
    </div>
    <ChartIndicatorSettingsModal
      v-model:open="indicatorSettingsOpen"
      :settings="indicatorSettings"
      :saving="indicatorSettingsSaving"
      :strategy-lines="strategyLines"
      @save="saveIndicatorSettings"
    />
  </section>
</template>
