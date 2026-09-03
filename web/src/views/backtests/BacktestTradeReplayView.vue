<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import BacktestEventDetails from './components/BacktestEventDetails.vue'
import JsonDetails from '@/features/backtests/JsonDetails.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import TradeReplayChartPanel from '@/features/backtests/TradeReplayChartPanel.vue'
import { formatDateTime, formatNumber, formatPercent, pnlClass } from '@/shared/format'
import { timestampMs } from '@/shared/time'
import { eventDisplayName, resolvePricePrecision } from './components/eventPresentation'

const route = useRoute()
const researchId = computed(() => typeof route.params.researchId === 'string' ? route.params.researchId : '')
const tradeId = computed(() => typeof route.params.tradeId === 'string' ? route.params.tradeId : '')

const routeReady = computed(() => Boolean(researchId.value && tradeId.value))
const tradeQuery = useQuery({ queryKey: computed(() => ['backtest-trade', researchId.value, tradeId.value]), queryFn: () => backtestApi.trade(researchId.value, tradeId.value), enabled: routeReady })
const eventsQuery = useQuery({ queryKey: computed(() => ['backtest-events', researchId.value, tradeId.value]), queryFn: () => backtestApi.events(researchId.value, tradeId.value), enabled: routeReady })
const strategyId = computed(() => tradeQuery.data.value?.strategy_id || '')
const schemaQuery = useQuery({ queryKey: computed(() => ['backtest-strategy-schema', strategyId.value]), queryFn: () => backtestApi.strategySchema(strategyId.value), enabled: computed(() => Boolean(strategyId.value)) })

const symbolPricePrecision = computed(() => {
  const trade = tradeQuery.data.value
  if (!trade) return 2
  const samplePrices: Array<number | string | null | undefined> = [
    trade.entry_price,
    trade.average_entry_price,
    trade.signal_price,
    trade.invalid_price,
    trade.exit_price
  ]
  if (Array.isArray(trade.tier_prices)) {
    samplePrices.push(...trade.tier_prices)
  }
  return resolvePricePrecision(trade.symbol, samplePrices)
})

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
const rootTo = computed(() => ({ path: '/backtests', query: route.query }))
const symbolsTo = computed(() => ({ path: `/backtests/${encodeURIComponent(researchId.value)}/symbols`, query: route.query }))
const equityTo = computed(() => ({ path: `/backtests/${encodeURIComponent(researchId.value)}/equity` }))
const displayTradeId = computed(() => tradeQuery.data.value?.trade_id || tradeId.value)
const signalId = computed(() => tradeQuery.data.value?.campaign_id || null)
const openedFromEquity = computed(() => route.query.from === 'equity')
const backTo = computed(() => {
  if (openedFromEquity.value) return equityTo.value
  return tradeQuery.data.value
    ? { path: `/backtests/${encodeURIComponent(researchId.value)}/symbols/${encodeURIComponent(tradeQuery.data.value.symbol)}/trades`, query: route.query }
    : symbolsTo.value
})
const crumbs = computed(() => openedFromEquity.value
  ? [{ label: '回测复盘', to: rootTo.value }, { label: '收益曲线', to: equityTo.value }, { label: '单笔复盘' }]
  : [{ label: '回测复盘', to: rootTo.value }, { label: '交易对数据', to: symbolsTo.value }, { label: '单笔复盘' }])
</script>

<template>
  <BacktestPage :title="tradeQuery.data.value ? `${tradeQuery.data.value.symbol} 单笔复盘` : '单笔复盘'"
    :eyebrow="displayTradeId" :back-to="backTo" :crumbs="crumbs">
    <QueryPanel :pending="tradeQuery.isPending.value" :error="tradeQuery.error.value" @retry="tradeQuery.refetch()">
      <template v-if="tradeQuery.data.value">
        <div class="trade-summary-strip">
          <div><span>首笔成交确认</span><strong>{{ formatDateTime(tradeQuery.data.value.entry_time) }}</strong></div>
          <div><span>开仓均价</span><strong>{{ formatNumber(tradeQuery.data.value.average_entry_price ??
            tradeQuery.data.value.entry_price, symbolPricePrecision) }}</strong></div>
          <div><span>退出时间</span><strong>{{ formatDateTime(tradeQuery.data.value.exit_time) }}</strong></div>
          <div><span>净盈亏</span><strong :class="pnlClass(tradeQuery.data.value.net_pnl)">{{
            formatNumber(tradeQuery.data.value.net_pnl) }} U</strong></div>
          <div><span>收益率</span><strong>{{ formatPercent(tradeQuery.data.value.net_return) }}</strong></div>
          <div class="fill-status"><span>成交档位</span><strong>{{ `已成交 ${filledTierCount} / ${tierDetails.length} 档`
              }}</strong></div>
          <div><span>退出原因</span><strong>{{ tradeQuery.data.value.exit_reason || '-' }}</strong></div>
          <div><span>交易 ID</span><strong class="trade-identity">{{ displayTradeId }}</strong></div>
          <div v-if="signalId"><span>信号 ID</span><strong class="trade-identity">{{ signalId }}</strong></div>
        </div>

        <TradeReplayChartPanel :trade="tradeQuery.data.value" :research-id="researchId"
          :overlays="schemaQuery.data.value?.chart_overlays" />

        <section class="detail-section trade-details-section">
          <h3>成交明细</h3>
          <a-descriptions :column="{ xs: 1, sm: 2, md: 2, lg: 4, xl: 4, xxl: 6 }" layout="vertical" bordered>
            <a-descriptions-item label="信号时间">{{ formatDateTime(tradeQuery.data.value.signal_time)
              }}</a-descriptions-item>
            <a-descriptions-item label="信号价格">{{ formatNumber(tradeQuery.data.value.signal_price, symbolPricePrecision)
              }}</a-descriptions-item>
            <a-descriptions-item label="失效价格">{{ formatNumber(tradeQuery.data.value.invalid_price, symbolPricePrecision)
              }}</a-descriptions-item>
            <a-descriptions-item v-for="tier in tierDetails" :key="tier.index"
              :label="tier.filled ? `${entrySideLabel}${tier.index}` : `限${entrySideLabel}${tier.index}`">
              <span class="tier-price">{{ formatNumber(tier.price, symbolPricePrecision) }}</span>
              <a-tag :color="tier.filled ? 'success' : 'default'" class="tier-status">{{ tier.filled ? '已成交' : '未成交'
                }}</a-tag>
              <span v-if="tier.filled" class="tier-times">
                <span>触发 K线 {{ formatDateTime(tier.triggerTime) }}</span>
                <span>确认 {{ formatDateTime(tier.confirmationTime) }}</span>
              </span>
            </a-descriptions-item>
          </a-descriptions>
        </section>
        <section class="detail-section timeline-section">
          <h3>事件时间线</h3>
          <QueryPanel :pending="eventsQuery.isPending.value" :error="eventsQuery.error.value"
            :empty="eventsQuery.data.value?.items.length === 0" @retry="eventsQuery.refetch()">
            <a-timeline class="timeline-events" aria-label="事件时间线"><a-timeline-item
                v-for="(event, index) in eventsQuery.data.value?.items" :key="event.id"
                :data-sequence="index + 1"><template #dot><span class="event-sequence"
                    :aria-label="`第 ${index + 1} 个事件`">{{ index + 1 }}</span></template>
                <div class="event-heading"><strong>{{ eventDisplayName(event) }}</strong><time>{{
                    formatDateTime(event.time) }}</time>
                </div>
                <BacktestEventDetails :event="event" :reference-data="allAttributes" :price-precision="symbolPricePrecision" />
              </a-timeline-item></a-timeline>
          </QueryPanel>
        </section>
        <section class="detail-section">
          <h3>策略扩展参数</h3><a-tag v-if="schemaQuery.data.value === null" color="orange" class="schema-fallback">策略 Schema
            不存在，显示原始
            JSON</a-tag>
          <JsonDetails :value="allAttributes"
            :groups="schemaQuery.data.value?.detail_groups || schemaQuery.data.value?.groups"
            :fields="schemaQuery.data.value?.parameter_fields || schemaQuery.data.value?.fields" />
        </section>
      </template>
    </QueryPanel>
  </BacktestPage>
</template>
