<script setup lang="ts">
import { computed, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { Database, Globe2, RefreshCw } from 'lucide-vue-next'
import {
  NButton, NDescriptions, NDescriptionsItem, NIcon, NRadioButton, NRadioGroup,
  NSpin, NTag, NTimeline, NTimelineItem, NTooltip
} from 'naive-ui'
import { useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import JsonDetails from '@/features/backtests/JsonDetails.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'
import { formatNumber, formatPercent, formatTime, pnlClass, timestampMs } from '@/features/backtests/format'

const intervals = ['1m', '5m', '15m', '1h', '4h', '6h', '8h', '12h', '1d']
const windowDays: Record<string, number> = { '1m': 1, '5m': 5, '15m': 14, '1h': 45, '4h': 120, '6h': 180, '8h': 240, '12h': 300, '1d': 540 }
const route = useRoute()
const researchId = computed(() => String(route.params.researchId))
const tradeId = computed(() => String(route.params.tradeId))
const interval = ref('5m')
const source = ref<'binance' | 'archive'>('binance')

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
  const padding = windowDays[interval.value] * 86_400_000
  return { symbol: trade.symbol, interval: interval.value, start_ms: entry - padding, end_ms: Math.max(entry, exit || entry) + padding, source: source.value }
})
const candlesQuery = useQuery({
  queryKey: computed(() => ['backtest-candles', candleParams.value]),
  queryFn: () => backtestApi.candles(candleParams.value!),
  enabled: computed(() => candleParams.value !== null),
  staleTime: 5 * 60_000
})
const allAttributes = computed(() => ({ ...(tradeQuery.data.value?.parameters || {}), ...(tradeQuery.data.value?.strategy_data || {}), ...(tradeQuery.data.value?.metrics || {}), ...(tradeQuery.data.value?.attributes || {}) }))
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
            <NRadioGroup v-model:value="interval" size="small"><NRadioButton v-for="item in intervals" :key="item" :value="item">{{ item }}</NRadioButton></NRadioGroup>
            <div class="source-tools">
              <NRadioGroup v-model:value="source" size="small">
                <NRadioButton value="binance"><NIcon :component="Globe2" /> Binance</NRadioButton>
                <NRadioButton value="archive"><NIcon :component="Database" /> 本地归档</NRadioButton>
              </NRadioGroup>
              <NTooltip><template #trigger><NButton circle quaternary aria-label="刷新K线" @click="candlesQuery.refetch()"><template #icon><NIcon :component="RefreshCw" /></template></NButton></template>刷新 K 线</NTooltip>
            </div>
          </div>
          <div v-if="candlesQuery.isPending.value" class="chart-loading"><NSpin /><span>加载 {{ source === 'binance' ? 'Binance' : '本地归档' }} K线</span></div>
          <QueryPanel v-else :error="candlesQuery.error.value" :empty="candlesQuery.data.value?.candles.length === 0" @retry="candlesQuery.refetch()">
            <TradeCandlestickChart :candles="candlesQuery.data.value?.candles || []" :trade="tradeQuery.data.value" :overlays="schemaQuery.data.value?.chart_overlays" />
          </QueryPanel>
          <div class="chart-legend"><NTag size="small" :bordered="false">{{ candlesQuery.data.value?.source || source }}</NTag><span>信号</span><span>三档挂单</span><span>实际成交</span><span>开仓均价</span><span>失效价</span><span>退出</span></div>
        </section>

        <div class="replay-details">
          <section class="detail-section">
            <h3>成交明细</h3>
            <NDescriptions :column="3" label-placement="top" bordered responsive="screen">
              <NDescriptionsItem label="信号时间">{{ formatTime(tradeQuery.data.value.signal_time) }}</NDescriptionsItem>
              <NDescriptionsItem label="信号价格">{{ formatNumber(tradeQuery.data.value.signal_price, 8) }}</NDescriptionsItem>
              <NDescriptionsItem label="失效价格">{{ formatNumber(tradeQuery.data.value.invalid_price, 8) }}</NDescriptionsItem>
              <NDescriptionsItem v-for="(price, index) in tradeQuery.data.value.tier_prices || []" :key="index" :label="`挂单 ${index + 1}`">{{ formatNumber(price, 8) }}</NDescriptionsItem>
            </NDescriptions>
          </section>
          <section class="detail-section timeline-section">
            <h3>事件时间线</h3>
            <QueryPanel :pending="eventsQuery.isPending.value" :error="eventsQuery.error.value" :empty="eventsQuery.data.value?.items.length === 0" @retry="eventsQuery.refetch()">
              <NTimeline><NTimelineItem v-for="event in eventsQuery.data.value?.items" :key="event.id || `${event.time}-${event.type}`" :title="event.title || event.type" :time="formatTime(event.time)" :content="event.description || (event.price != null ? `价格 ${formatNumber(event.price, 8)}` : '')" /></NTimeline>
            </QueryPanel>
          </section>
        </div>
        <section class="detail-section"><h3>策略扩展参数</h3><NTag v-if="schemaQuery.data.value === null" size="small" :bordered="false" type="warning" class="schema-fallback">策略 Schema 不存在，显示原始 JSON</NTag><JsonDetails :value="allAttributes" :groups="schemaQuery.data.value?.detail_groups || schemaQuery.data.value?.groups" :fields="schemaQuery.data.value?.parameter_fields || schemaQuery.data.value?.fields" /></section>
      </template>
    </QueryPanel>
  </BacktestPage>
</template>
