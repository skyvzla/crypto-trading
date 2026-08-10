<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { Database, Globe2, RefreshCw } from 'lucide-vue-next'
import { useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
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
const researchId = computed(() => String(route.params.researchId))
const tradeId = computed(() => String(route.params.tradeId))
const interval = ref('5m')
const source = ref<'binance' | 'archive'>('binance')
const windowShiftBars = ref(0)

const tradeQuery = useQuery({ queryKey: computed(() => ['backtest-trade', researchId.value, tradeId.value]), queryFn: () => backtestApi.trade(researchId.value, tradeId.value) })
const eventsQuery = useQuery({ queryKey: computed(() => ['backtest-events', researchId.value, tradeId.value]), queryFn: () => backtestApi.events(researchId.value, tradeId.value) })
const strategyId = computed(() => tradeQuery.data.value?.strategy_id || '')
const schemaQuery = useQuery({ queryKey: computed(() => ['backtest-strategy-schema', strategyId.value]), queryFn: () => backtestApi.strategySchema(strategyId.value), enabled: computed(() => Boolean(strategyId.value)) })

const candleParams = computed(() => {
  const trade = tradeQuery.data.value
  if (!trade) return null
  const entry = timestampMs(trade.entry_time)
  const exit = timestampMs(trade.exit_time) ?? entry
  if (entry === null) return null
  const points = [entry, exit, timestampMs(trade.signal_time)].filter((value): value is number => value !== null)
  // 在线合约接口按单次上限取 1500 根；本地归档取 5000 根。
  const focus = points.reduce((sum, value) => sum + value, 0) / points.length
  const halfWindowBars = source.value === 'binance' ? 750 : 2500
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
  staleTime: 5 * 60_000
})
function selectInterval(value: string) {
  interval.value = value
  windowShiftBars.value = 0
  if (value === '1s') source.value = 'archive'
}
function requestMore(direction: 'before' | 'after') {
  const shiftBars = source.value === 'binance' ? 750 : 2500
  windowShiftBars.value += direction === 'before' ? -shiftBars : shiftBars
}
watch(source, () => { windowShiftBars.value = 0 })
const allAttributes = computed(() => ({ ...(tradeQuery.data.value?.parameters || {}), ...(tradeQuery.data.value?.strategy_data || {}), ...(tradeQuery.data.value?.metrics || {}), ...(tradeQuery.data.value?.attributes || {}) }))
function eventContent(event: { data?: Record<string, unknown>; description?: string | null; price?: number | null }): string {
  if (event.description) return event.description
  if (event.price != null) return `价格 ${formatNumber(event.price, 8)}`
  return event.data && Object.keys(event.data).length ? JSON.stringify(event.data) : ''
}
const backTo = computed(() => tradeQuery.data.value ? `/backtests/${encodeURIComponent(researchId.value)}/symbols/${encodeURIComponent(tradeQuery.data.value.symbol)}/trades` : `/backtests/${encodeURIComponent(researchId.value)}/symbols`)
</script>

<template>
  <BacktestPage :title="tradeQuery.data.value ? `${tradeQuery.data.value.symbol} 单笔复盘` : '单笔复盘'" :eyebrow="tradeId" :back-to="backTo" :crumbs="[{ label: '回测复盘', to: '/backtests' }, { label: '交易对数据', to: `/backtests/${researchId}/symbols` }, { label: '单笔复盘' }]">
    <QueryPanel :pending="tradeQuery.isPending.value" :error="tradeQuery.error.value" @retry="tradeQuery.refetch()">
      <template v-if="tradeQuery.data.value">
        <div class="trade-summary-strip">
          <div><span>开仓时间</span><strong>{{ formatTime(tradeQuery.data.value.entry_time) }}</strong></div>
          <div><span>开仓均价</span><strong>{{ formatNumber(tradeQuery.data.value.average_entry_price ?? tradeQuery.data.value.entry_price, 8) }}</strong></div>
          <div><span>退出时间</span><strong>{{ formatTime(tradeQuery.data.value.exit_time) }}</strong></div>
          <div><span>净盈亏</span><strong :class="pnlClass(tradeQuery.data.value.net_pnl)">{{ formatNumber(tradeQuery.data.value.net_pnl) }} U</strong></div>
          <div><span>收益率</span><strong>{{ formatPercent(tradeQuery.data.value.net_return) }}</strong></div>
          <div><span>退出原因</span><strong>{{ tradeQuery.data.value.exit_reason || '-' }}</strong></div>
        </div>

        <section class="chart-section">
          <div class="chart-toolbar">
            <a-radio-group :value="interval" size="small" @change="(event: { target: { value: string } }) => selectInterval(event.target.value)"><a-radio-button v-for="item in intervals" :key="item" :value="item">{{ item }}</a-radio-button></a-radio-group>
            <div class="source-tools">
              <a-radio-group v-model:value="source" size="small">
                <a-radio-button value="binance"><Globe2 :size="14" /> Binance</a-radio-button>
                <a-radio-button value="archive"><Database :size="14" /> 本地归档</a-radio-button>
              </a-radio-group>
              <a-tooltip title="刷新 K 线"><a-button type="text" shape="circle" aria-label="刷新K线" @click="candlesQuery.refetch()"><template #icon><RefreshCw :size="16" /></template></a-button></a-tooltip>
            </div>
          </div>
          <div v-if="candlesQuery.isPending.value" class="chart-loading"><a-spin /><span>加载 {{ source === 'binance' ? 'Binance' : '本地归档' }} K线</span></div>
          <QueryPanel v-else :error="candlesQuery.error.value" :empty="candlesQuery.data.value?.candles.length === 0" @retry="candlesQuery.refetch()">
            <TradeCandlestickChart :candles="candlesQuery.data.value?.candles || []" :trade="tradeQuery.data.value" :overlays="schemaQuery.data.value?.chart_overlays" @request-more="requestMore" />
          </QueryPanel>
          <div class="chart-legend"><a-tag color="blue">{{ candlesQuery.data.value?.source || source }}</a-tag><span>信号</span><span>三档挂单</span><span>实际成交</span><span>开仓均价</span><span>失效价</span><span>退出</span></div>
        </section>

        <div class="replay-details">
          <section class="detail-section">
            <h3>成交明细</h3>
            <a-descriptions :column="3" layout="vertical" bordered>
              <a-descriptions-item label="信号时间">{{ formatTime(tradeQuery.data.value.signal_time) }}</a-descriptions-item>
              <a-descriptions-item label="信号价格">{{ formatNumber(tradeQuery.data.value.signal_price, 8) }}</a-descriptions-item>
              <a-descriptions-item label="失效价格">{{ formatNumber(tradeQuery.data.value.invalid_price, 8) }}</a-descriptions-item>
              <a-descriptions-item v-for="(price, index) in tradeQuery.data.value.tier_prices || []" :key="index" :label="`挂单 ${index + 1}`">{{ formatNumber(price, 8) }}</a-descriptions-item>
            </a-descriptions>
          </section>
          <section class="detail-section timeline-section">
            <h3>事件时间线</h3>
            <QueryPanel :pending="eventsQuery.isPending.value" :error="eventsQuery.error.value" :empty="eventsQuery.data.value?.items.length === 0" @retry="eventsQuery.refetch()">
              <a-timeline><a-timeline-item v-for="event in eventsQuery.data.value?.items" :key="event.id" :label="formatTime(event.time)"><strong>{{ event.title || event.type }}</strong><div>{{ eventContent(event) }}</div></a-timeline-item></a-timeline>
            </QueryPanel>
          </section>
        </div>
        <section class="detail-section"><h3>策略扩展参数</h3><a-tag v-if="schemaQuery.data.value === null" color="orange" class="schema-fallback">策略 Schema 不存在，显示原始 JSON</a-tag><JsonDetails :value="allAttributes" :groups="schemaQuery.data.value?.detail_groups || schemaQuery.data.value?.groups" :fields="schemaQuery.data.value?.parameter_fields || schemaQuery.data.value?.fields" /></section>
      </template>
    </QueryPanel>
  </BacktestPage>
</template>
