<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { ArrowDownToLine, ArrowUpToLine, Database, Globe2, Maximize2, Minimize2, RefreshCw, RotateCcw, SlidersHorizontal } from 'lucide-vue-next'
import { backtestApi } from '@/api/backtests'
import type { BacktestCandle, ChartOverlay } from '@/api/types'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'
import { timestampMs } from './format'
import type { TradeChartData, TradeChartFillDisplay, TradeChartFillTimeSemantics } from './tradeChart'

const intervals = ['1s', '1m', '5m', '15m', '1h', '4h', '6h', '8h', '12h', '1d']
const intervalMs: Record<string, number> = {
  '1s': 1_000,
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '6h': 21_600_000,
  '8h': 28_800_000,
  '12h': 43_200_000,
  '1d': 86_400_000
}

const props = withDefaults(defineProps<{
  trade: TradeChartData
  mode?: 'backtest' | 'market'
  researchId?: string
  overlays?: ChartOverlay[]
  fillDisplay?: TradeChartFillDisplay
  fillTimeSemantics?: TradeChartFillTimeSemantics
  exitLabel?: string
  strategyLines?: boolean
}>(), {
  mode: 'backtest',
  fillDisplay: 'entry',
  fillTimeSemantics: 'backtest-confirmation',
  exitLabel: '退出成交',
  strategyLines: true
})

const interval = ref('5m')
const windowShiftBars = ref(0)
const chartRef = ref<InstanceType<typeof TradeCandlestickChart> | null>(null)
const chartSection = ref<HTMLElement | null>(null)
const isFullscreen = ref(false)
const indicators = ref({ volume: true, macd: false, ema: false, kdj: false })
const lineVisibility = ref({ signal: true, tiers: true, average: true, invalid: true, extensions: true })
const focusTimeMs = ref<number | null>(null)
const loadedCandles = ref<BacktestCandle[]>([])

const availableIntervals = computed(() => props.mode === 'market'
  ? intervals.filter((item) => item !== '1s')
  : intervals
)
const source = computed<'binance' | 'archive'>(() => props.mode === 'market' || interval.value !== '1s' ? 'binance' : 'archive')
const sourceLabel = computed(() => source.value === 'archive' ? '本地归档' : 'Binance')
const isReady = computed(() => Boolean(
  props.trade.symbol
  && timestampMs(props.trade.entry_time) !== null
  && (props.mode === 'market' || props.researchId)
))
const candleParams = computed(() => {
  if (!isReady.value) return null
  const entry = timestampMs(props.trade.entry_time)
  if (entry === null) return null
  const focus = focusTimeMs.value ?? entry
  const halfWindowBars = 750
  const padding = intervalMs[interval.value] * halfWindowBars
  const windowCenter = Math.max(padding, focus + windowShiftBars.value * intervalMs[interval.value])
  return {
    ...(props.mode === 'backtest' ? { research_id: props.researchId } : {}),
    symbol: props.trade.symbol,
    interval: interval.value,
    start_ms: windowCenter - padding,
    end_ms: windowCenter + padding,
    source: source.value
  }
})
const candlesQuery = useQuery({
  queryKey: computed(() => ['trade-replay-candles', props.mode, props.researchId, props.trade.symbol, candleParams.value]),
  queryFn: () => backtestApi.candles(candleParams.value!),
  enabled: computed(() => candleParams.value !== null),
  staleTime: 5 * 60_000,
  placeholderData: (previous) => previous
})

function selectInterval(value: string) {
  interval.value = value
  windowShiftBars.value = 0
}

async function focusTradeEvent(kind: 'entry' | 'exit') {
  const firstFillTime = props.trade.fills?.[0]?.time
  const target = timestampMs(kind === 'entry' ? (firstFillTime ?? props.trade.entry_time) : props.trade.exit_time)
  if (target === null) return
  focusTimeMs.value = target
  windowShiftBars.value = 0
  await nextTick()
  if (kind === 'entry') chartRef.value?.focusEntry?.()
  else chartRef.value?.focusExit?.()
}

function requestMore(direction: 'before' | 'after') {
  const shiftBars = 750
  windowShiftBars.value += direction === 'before' ? -shiftBars : shiftBars
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

onMounted(() => document.addEventListener('fullscreenchange', syncFullscreenState))
onBeforeUnmount(() => document.removeEventListener('fullscreenchange', syncFullscreenState))

watch(availableIntervals, (items) => {
  if (!items.includes(interval.value)) interval.value = items[0] || '5m'
}, { immediate: true })
watch(() => candlesQuery.data.value, (response) => {
  if (!response || response.interval !== interval.value || response.source !== source.value) return
  const byTime = new Map(loadedCandles.value.map((candle) => [candle.time, candle]))
  response.candles.forEach((candle) => byTime.set(candle.time, candle))
  loadedCandles.value = [...byTime.values()].sort((left, right) => left.time - right.time)
}, { immediate: true })
watch(() => [props.trade.symbol, props.trade.entry_time, props.trade.exit_time, interval.value, source.value], () => {
  loadedCandles.value = []
  windowShiftBars.value = 0
})
watch(() => props.trade.entry_time, () => { focusTimeMs.value = null })
</script>

<template>
  <section ref="chartSection" class="chart-section">
    <div class="chart-toolbar">
      <a-radio-group :value="interval" size="small" @change="(event: { target: { value: string } }) => selectInterval(event.target.value)">
        <a-radio-button v-for="item in availableIntervals" :key="item" :value="item">{{ item }}</a-radio-button>
      </a-radio-group>
      <div class="source-tools">
        <a-tooltip :title="mode === 'market' ? 'Binance 公开 K 线；买卖点来自账本成交' : undefined">
          <a-tag color="blue"><Database v-if="source === 'archive'" :size="14" /> <Globe2 v-else :size="14" /> {{ sourceLabel }}</a-tag>
        </a-tooltip>
        <a-divider type="vertical" />
        <a-checkbox v-model:checked="indicators.volume">VOL</a-checkbox>
        <a-checkbox v-model:checked="indicators.macd">MACD</a-checkbox>
        <a-checkbox v-model:checked="indicators.ema">EMA</a-checkbox>
        <a-checkbox v-model:checked="indicators.kdj">KDJ</a-checkbox>
        <a-dropdown :trigger="['click']">
          <a-tooltip title="标线显示"><a-button type="text" shape="circle" class="chart-icon-button" aria-label="标线显示"><template #icon><SlidersHorizontal :size="16" /></template></a-button></a-tooltip>
          <template #overlay>
            <a-card size="small" :bordered="false" @click.stop>
              <a-space direction="vertical" :size="8">
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.signal">信号价</a-checkbox>
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.tiers">限价与成交档位</a-checkbox>
                <a-checkbox v-model:checked="lineVisibility.average">开仓均价</a-checkbox>
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.invalid">失效价</a-checkbox>
                <a-checkbox v-if="strategyLines" v-model:checked="lineVisibility.extensions">策略扩展价位</a-checkbox>
              </a-space>
            </a-card>
          </template>
        </a-dropdown>
        <a-divider type="vertical" />
        <a-tooltip title="跳转到第一笔成交"><a-button type="text" class="chart-tool-button" aria-label="跳转到第一笔成交" @click="focusTradeEvent('entry')"><template #icon><ArrowUpToLine :size="15" /></template>首笔成交</a-button></a-tooltip>
        <a-tooltip :title="`跳转到${exitLabel}`"><a-button type="text" class="chart-tool-button" :aria-label="`跳转到${exitLabel}`" @click="focusTradeEvent('exit')"><template #icon><ArrowDownToLine :size="15" /></template>{{ exitLabel }}</a-button></a-tooltip>
        <a-spin v-if="candlesQuery.isFetching.value" size="small" />
        <a-tooltip title="刷新 K 线"><a-button type="text" shape="circle" class="chart-icon-button" aria-label="刷新K线" @click="candlesQuery.refetch()"><template #icon><RefreshCw :size="16" /></template></a-button></a-tooltip>
        <a-tooltip title="恢复默认尺寸"><a-button type="text" shape="circle" class="chart-icon-button" aria-label="恢复默认尺寸" @click="resetChartSize"><template #icon><RotateCcw :size="16" /></template></a-button></a-tooltip>
        <a-tooltip :title="isFullscreen ? '退出全屏' : '全屏查看'"><a-button type="text" shape="circle" class="chart-icon-button" :aria-label="isFullscreen ? '退出全屏' : '全屏查看'" @click="toggleFullscreen"><template #icon><Minimize2 v-if="isFullscreen" :size="16" /><Maximize2 v-else :size="16" /></template></a-button></a-tooltip>
      </div>
    </div>
    <div v-if="candlesQuery.isFetching.value && loadedCandles.length === 0" class="chart-loading"><a-spin /><span>加载 {{ sourceLabel }} K线</span></div>
    <QueryPanel v-else :error="loadedCandles.length ? null : candlesQuery.error.value" :empty="loadedCandles.length === 0" @retry="candlesQuery.refetch()">
      <TradeCandlestickChart ref="chartRef" :candles="loadedCandles" :trade="trade" :overlays="overlays" :indicators="indicators" :line-visibility="lineVisibility" :focus-time="focusTimeMs" :fill-display="fillDisplay" :fill-time-semantics="fillTimeSemantics" @request-more="requestMore" />
    </QueryPanel>
    <div class="chart-legend">
      <a-tag color="blue">{{ candlesQuery.data.value?.source === 'archive' ? '本地归档' : 'Binance' }}</a-tag>
      <template v-if="fillDisplay === 'all'">
        <span class="legend-item filled"><i />买入</span>
        <span class="legend-item invalid"><i />卖出</span>
        <span class="legend-item average"><i />开仓均价</span>
      </template>
      <template v-else>
        <span class="legend-item signal"><i />信号</span>
        <span class="legend-item pending"><i />未成交挂单</span>
        <span class="legend-item filled"><i />实际成交</span>
        <span class="legend-item average"><i />开仓均价</span>
        <span class="legend-item invalid"><i />失效价</span>
        <span class="legend-item exit"><i />退出</span>
      </template>
    </div>
  </section>
</template>
