<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { ArrowDownToLine, ArrowUpToLine, Database, Globe2, Maximize2, Minimize2, RefreshCw, RotateCcw, SlidersHorizontal } from 'lucide-vue-next'
import { useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestCandle } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import JsonDetails from '@/features/backtests/JsonDetails.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'
import { formatNumber, formatPercent, formatTime, pnlClass, timestampMs } from '@/features/backtests/format'

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
const route = useRoute()
const researchId = computed(() => typeof route.params.researchId === 'string' ? route.params.researchId : '')
const tradeId = computed(() => typeof route.params.tradeId === 'string' ? route.params.tradeId : '')
const interval = ref('5m')
const source = computed<'binance' | 'archive'>(() => interval.value === '1s' ? 'archive' : 'binance')
const sourceLabel = computed(() => source.value === 'archive' ? '本地归档' : 'Binance')
const windowShiftBars = ref(0)
const chartRef = ref<InstanceType<typeof TradeCandlestickChart> | null>(null)
const chartSection = ref<HTMLElement | null>(null)
const isFullscreen = ref(false)
const indicators = ref({ volume: true, macd: false, ema: false, kdj: false })
const lineVisibility = ref({ signal: true, tiers: true, average: true, invalid: true, extensions: true })
const focusTimeMs = ref<number | null>(null)
const loadedCandles = ref<BacktestCandle[]>([])

const routeReady = computed(() => Boolean(researchId.value && tradeId.value))
const tradeQuery = useQuery({ queryKey: computed(() => ['backtest-trade', researchId.value, tradeId.value]), queryFn: () => backtestApi.trade(researchId.value, tradeId.value), enabled: routeReady })
const eventsQuery = useQuery({ queryKey: computed(() => ['backtest-events', researchId.value, tradeId.value]), queryFn: () => backtestApi.events(researchId.value, tradeId.value), enabled: routeReady })
const strategyId = computed(() => tradeQuery.data.value?.strategy_id || '')
const schemaQuery = useQuery({ queryKey: computed(() => ['backtest-strategy-schema', strategyId.value]), queryFn: () => backtestApi.strategySchema(strategyId.value), enabled: computed(() => Boolean(strategyId.value)) })

const candleParams = computed(() => {
  const trade = tradeQuery.data.value
  if (!trade) return null
  const entry = timestampMs(trade.entry_time)
  if (entry === null) return null
  // 以第一笔实际成交为主参考，退出较晚时也必须保证入场附近可见。
  const focus = focusTimeMs.value ?? entry
  const halfWindowBars = 750
  const padding = intervalMs[interval.value] * halfWindowBars
  const windowCenter = Math.max(padding, focus + windowShiftBars.value * intervalMs[interval.value])
  return {
    research_id: researchId.value,
    symbol: trade.symbol,
    interval: interval.value,
    start_ms: windowCenter - padding,
    end_ms: windowCenter + padding,
    source: source.value
  }
})
const candlesQuery = useQuery({
  queryKey: computed(() => ['backtest-candles', candleParams.value]),
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
  const trade = tradeQuery.data.value
  if (!trade) return
  const firstFillTime = trade.fills?.[0]?.time
  const target = timestampMs(kind === 'entry' ? (firstFillTime ?? trade.entry_time) : trade.exit_time)
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
watch(() => candlesQuery.data.value, (response) => {
  if (!response) return
  if (response.interval !== interval.value || response.source !== source.value) return
  const byTime = new Map(loadedCandles.value.map((candle) => [candle.time, candle]))
  response.candles.forEach((candle) => byTime.set(candle.time, candle))
  loadedCandles.value = [...byTime.values()].sort((left, right) => left.time - right.time)
}, { immediate: true })
watch(() => [tradeId.value, interval.value, source.value], () => {
  loadedCandles.value = []
  windowShiftBars.value = 0
})
watch(tradeId, () => { focusTimeMs.value = null })
const allAttributes = computed(() => ({ ...(tradeQuery.data.value?.parameters || {}), ...(tradeQuery.data.value?.strategy_data || {}), ...(tradeQuery.data.value?.metrics || {}), ...(tradeQuery.data.value?.attributes || {}) }))
const entrySideLabel = computed(() => {
  const side = String(tradeQuery.data.value?.side || '').toLowerCase()
  return side.includes('short') || side === 'sell' ? '卖' : '买'
})
const entryFills = computed(() => {
  const trade = tradeQuery.data.value
  if (!trade) return []
  const expectedSide = entrySideLabel.value === '卖' ? 'sell' : 'buy'
  return (trade.fills || []).filter((fill) => fill.side?.toLowerCase() === expectedSide)
})
function triggerCandleTime(fillTime: string | number): number | null {
  const confirmationTime = timestampMs(fillTime)
  // 引擎在 1 秒 K 线收齐时记录 fill_time；实际触价发生在该 K 线区间内。
  return confirmationTime === null ? null : confirmationTime - 1_000
}
function samePrice(left: number, right: number): boolean {
  return Math.abs(left - right) <= Math.max(1e-12, Math.max(Math.abs(left), Math.abs(right)) * 1e-9)
}
const tierDetails = computed(() => {
  const trade = tradeQuery.data.value
  if (!trade) return []
  const prices = trade.tier_prices?.length ? trade.tier_prices : (trade.orders || []).map((order) => order.price)
  return prices.map((price, index) => {
    const fill = entryFills.value.find((item) => samePrice(item.price, price))
    return {
      index: index + 1,
      price: fill?.price ?? price,
      filled: Boolean(fill),
      confirmationTime: fill?.time ?? null,
      triggerTime: fill ? triggerCandleTime(fill.time) : null
    }
  })
})
const filledTierCount = computed(() => tierDetails.value.filter((item) => item.filled).length)
function eventContent(event: { data?: Record<string, unknown>; description?: string | null; price?: number | null }): string {
  if (event.description) return event.description
  if (event.price != null) return `价格 ${formatNumber(event.price, 8)}`
  return event.data && Object.keys(event.data).length ? JSON.stringify(event.data) : ''
}
const rootTo = computed(() => ({ path: '/backtests', query: route.query }))
const symbolsTo = computed(() => ({ path: `/backtests/${encodeURIComponent(researchId.value)}/symbols`, query: route.query }))
const backTo = computed(() => tradeQuery.data.value
  ? { path: `/backtests/${encodeURIComponent(researchId.value)}/symbols/${encodeURIComponent(tradeQuery.data.value.symbol)}/trades`, query: route.query }
  : symbolsTo.value)
</script>

<template>
  <BacktestPage :title="tradeQuery.data.value ? `${tradeQuery.data.value.symbol} 单笔复盘` : '单笔复盘'" :eyebrow="tradeId" :back-to="backTo" :crumbs="[{ label: '回测复盘', to: rootTo }, { label: '交易对数据', to: symbolsTo }, { label: '单笔复盘' }]">
    <QueryPanel :pending="tradeQuery.isPending.value" :error="tradeQuery.error.value" @retry="tradeQuery.refetch()">
      <template v-if="tradeQuery.data.value">
        <div class="trade-summary-strip">
          <div><span>首笔成交确认</span><strong>{{ formatTime(tradeQuery.data.value.entry_time) }}</strong></div>
          <div><span>开仓均价</span><strong>{{ formatNumber(tradeQuery.data.value.average_entry_price ?? tradeQuery.data.value.entry_price, 8) }}</strong></div>
          <div><span>退出时间</span><strong>{{ formatTime(tradeQuery.data.value.exit_time) }}</strong></div>
          <div><span>净盈亏</span><strong :class="pnlClass(tradeQuery.data.value.net_pnl)">{{ formatNumber(tradeQuery.data.value.net_pnl) }} U</strong></div>
          <div><span>收益率</span><strong>{{ formatPercent(tradeQuery.data.value.net_return) }}</strong></div>
          <div class="fill-status"><span>成交档位</span><strong>{{ `已成交 ${filledTierCount} / ${tierDetails.length} 档` }}</strong></div>
          <div><span>退出原因</span><strong>{{ tradeQuery.data.value.exit_reason || '-' }}</strong></div>
        </div>

        <section ref="chartSection" class="chart-section">
          <div class="chart-toolbar">
            <a-radio-group :value="interval" size="small" @change="(event: { target: { value: string } }) => selectInterval(event.target.value)"><a-radio-button v-for="item in intervals" :key="item" :value="item">{{ item }}</a-radio-button></a-radio-group>
            <div class="source-tools">
              <a-tag color="blue"><Database v-if="source === 'archive'" :size="14" /> <Globe2 v-else :size="14" /> {{ sourceLabel }}</a-tag>
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
                      <a-checkbox v-model:checked="lineVisibility.signal">信号价</a-checkbox>
                      <a-checkbox v-model:checked="lineVisibility.tiers">限价与成交档位</a-checkbox>
                      <a-checkbox v-model:checked="lineVisibility.average">开仓均价</a-checkbox>
                      <a-checkbox v-model:checked="lineVisibility.invalid">失效价</a-checkbox>
                      <a-checkbox v-model:checked="lineVisibility.extensions">策略扩展价位</a-checkbox>
                    </a-space>
                  </a-card>
                </template>
              </a-dropdown>
              <a-divider type="vertical" />
              <a-tooltip title="跳转到第一笔成交"><a-button type="text" class="chart-tool-button" aria-label="跳转到第一笔成交" @click="focusTradeEvent('entry')"><template #icon><ArrowUpToLine :size="15" /></template>首笔成交</a-button></a-tooltip>
              <a-tooltip title="跳转到退出成交"><a-button type="text" class="chart-tool-button" aria-label="跳转到退出成交" @click="focusTradeEvent('exit')"><template #icon><ArrowDownToLine :size="15" /></template>退出成交</a-button></a-tooltip>
              <a-spin v-if="candlesQuery.isFetching.value" size="small" />
              <a-tooltip title="刷新 K 线"><a-button type="text" shape="circle" class="chart-icon-button" aria-label="刷新K线" @click="candlesQuery.refetch()"><template #icon><RefreshCw :size="16" /></template></a-button></a-tooltip>
              <a-tooltip title="恢复默认尺寸"><a-button type="text" shape="circle" class="chart-icon-button" aria-label="恢复默认尺寸" @click="resetChartSize"><template #icon><RotateCcw :size="16" /></template></a-button></a-tooltip>
              <a-tooltip :title="isFullscreen ? '退出全屏' : '全屏查看'"><a-button type="text" shape="circle" class="chart-icon-button" :aria-label="isFullscreen ? '退出全屏' : '全屏查看'" @click="toggleFullscreen"><template #icon><Minimize2 v-if="isFullscreen" :size="16" /><Maximize2 v-else :size="16" /></template></a-button></a-tooltip>
            </div>
          </div>
          <div v-if="candlesQuery.isFetching.value && loadedCandles.length === 0" class="chart-loading"><a-spin /><span>加载 {{ sourceLabel }} K线</span></div>
          <QueryPanel v-else :error="loadedCandles.length ? null : candlesQuery.error.value" :empty="loadedCandles.length === 0" @retry="candlesQuery.refetch()">
            <TradeCandlestickChart ref="chartRef" :candles="loadedCandles" :trade="tradeQuery.data.value" :overlays="schemaQuery.data.value?.chart_overlays" :indicators="indicators" :line-visibility="lineVisibility" :focus-time="focusTimeMs" @request-more="requestMore" />
          </QueryPanel>
          <div class="chart-legend">
            <a-tag color="blue">{{ candlesQuery.data.value?.source === 'archive' ? '本地归档' : 'Binance' }}</a-tag>
            <span class="legend-item signal"><i />信号</span>
            <span class="legend-item pending"><i />未成交挂单</span>
            <span class="legend-item filled"><i />实际成交</span>
            <span class="legend-item average"><i />开仓均价</span>
            <span class="legend-item invalid"><i />失效价</span>
            <span class="legend-item exit"><i />退出</span>
          </div>
        </section>

        <div class="replay-details">
          <section class="detail-section">
            <h3>成交明细</h3>
            <a-descriptions :column="3" layout="vertical" bordered>
              <a-descriptions-item label="信号时间">{{ formatTime(tradeQuery.data.value.signal_time) }}</a-descriptions-item>
              <a-descriptions-item label="信号价格">{{ formatNumber(tradeQuery.data.value.signal_price, 8) }}</a-descriptions-item>
              <a-descriptions-item label="失效价格">{{ formatNumber(tradeQuery.data.value.invalid_price, 8) }}</a-descriptions-item>
              <a-descriptions-item v-for="tier in tierDetails" :key="tier.index" :label="tier.filled ? `${entrySideLabel}${tier.index}` : `限${entrySideLabel}${tier.index}`">
                <span class="tier-price">{{ formatNumber(tier.price, 8) }}</span>
                <a-tag :color="tier.filled ? 'success' : 'default'" class="tier-status">{{ tier.filled ? '已成交' : '未成交' }}</a-tag>
                <span v-if="tier.filled" class="tier-times">
                  <span>触发 K线 {{ formatTime(tier.triggerTime) }}</span>
                  <span>确认 {{ formatTime(tier.confirmationTime) }}</span>
                </span>
              </a-descriptions-item>
            </a-descriptions>
          </section>
          <section class="detail-section timeline-section">
            <h3>事件时间线</h3>
            <QueryPanel :pending="eventsQuery.isPending.value" :error="eventsQuery.error.value" :empty="eventsQuery.data.value?.items.length === 0" @retry="eventsQuery.refetch()">
              <a-timeline><a-timeline-item v-for="event in eventsQuery.data.value?.items" :key="event.id"><div class="event-heading"><strong>{{ event.title || event.type }}</strong><time>{{ formatTime(event.time) }}</time></div><div class="event-content">{{ eventContent(event) }}</div></a-timeline-item></a-timeline>
            </QueryPanel>
          </section>
        </div>
        <section class="detail-section"><h3>策略扩展参数</h3><a-tag v-if="schemaQuery.data.value === null" color="orange" class="schema-fallback">策略 Schema 不存在，显示原始 JSON</a-tag><JsonDetails :value="allAttributes" :groups="schemaQuery.data.value?.detail_groups || schemaQuery.data.value?.groups" :fields="schemaQuery.data.value?.parameter_fields || schemaQuery.data.value?.fields" /></section>
      </template>
    </QueryPanel>
  </BacktestPage>
</template>
