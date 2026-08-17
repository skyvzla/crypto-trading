<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type TableColumnsType } from 'ant-design-vue'
import { ArrowLeft, CircleDotDashed } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { CampaignPnL, LedgerTrade, StrategyAuditEvent } from '@/api/types'
import TradeReplayChartPanel from '@/features/backtests/TradeReplayChartPanel.vue'
import type { TradeChartData } from '@/features/backtests/tradeChart'
import DataState from '@/features/operations/DataState.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { collectPageItems } from '@/features/operations/pagination'
import { formatDateTime, formatMoney, pnlClass, sideLabel } from '@/features/operations/format'

const route = useRoute()
const router = useRouter()
const campaignId = computed(() => typeof route.params.campaignId === 'string' ? route.params.campaignId : '')
const accountId = computed(() => String(route.query.account_id ?? '').trim())
const strategyId = computed(() => String(route.query.strategy_id ?? '').trim())
const symbol = computed(() => String(route.query.symbol ?? '').trim())
const routeReady = computed(() => Boolean(campaignId.value && accountId.value && strategyId.value && symbol.value))
const routeKey = computed(() => `${accountId.value}:${strategyId.value}:${symbol.value}:${campaignId.value}`)
const loading = ref(false)
const error = ref<string | null>(null)
const campaignPnl = ref<CampaignPnL | null>(null)
const campaignPnlError = ref<string | null>(null)
const fills = ref<LedgerTrade[]>([])
const fillError = ref<string | null>(null)
const events = ref<StrategyAuditEvent[]>([])
const eventError = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)

const sortedFills = computed(() => [...fills.value].sort((left, right) => {
  const leftTime = Date.parse(left.exchange_time)
  const rightTime = Date.parse(right.exchange_time)
  return (Number.isNaN(leftTime) ? 0 : leftTime) - (Number.isNaN(rightTime) ? 0 : rightTime)
}))

function numberValue(value: string | number | null | undefined): number | null {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

const campaignChartTrade = computed<TradeChartData | null>(() => {
  const [firstFill] = sortedFills.value
  const lastFill = sortedFills.value.at(-1)
  if (!firstFill || !lastFill) return null
  const firstSide = String(firstFill.side).toUpperCase()
  const firstPrice = numberValue(firstFill.price)
  const lastPrice = numberValue(lastFill.price)
  if (firstPrice === null || lastPrice === null) return null
  const averageEntry = firstSide === 'BUY'
    ? numberValue(campaignPnl.value?.buy_avg_price)
    : numberValue(campaignPnl.value?.sell_avg_price)
  return {
    symbol: symbol.value,
    side: firstSide,
    entry_time: firstFill.exchange_time,
    entry_price: firstPrice,
    average_entry_price: averageEntry ?? firstPrice,
    exit_time: lastFill.exchange_time,
    exit_price: lastPrice,
    net_pnl: numberValue(campaignPnl.value?.net_realized_pnl) ?? 0,
    fills: sortedFills.value.flatMap((fill) => {
      const price = numberValue(fill.price)
      return price === null ? [] : [{
        id: String(fill.id),
        time: fill.exchange_time,
        price,
        quantity: numberValue(fill.quantity),
        side: fill.side
      }]
    })
  }
})

const fillColumns: TableColumnsType<LedgerTrade> = [
  { title: '时间', dataIndex: 'exchange_time', key: 'time', width: 180, customRender: ({ text }) => formatDateTime(String(text)) },
  { title: '方向', dataIndex: 'side', key: 'side', width: 105, customRender: ({ text }) => sideLabel(String(text)) },
  { title: '价格', dataIndex: 'price', key: 'price', width: 130, customRender: ({ text }) => formatMoney(String(text), 6) },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 120 },
  { title: '已实现 PnL', dataIndex: 'realized_pnl', key: 'pnl', width: 125, customCell: (record) => ({ class: pnlClass(record.realized_pnl) }), customRender: ({ text }) => formatMoney(text == null ? null : String(text)) },
  { title: '手续费', key: 'fee', width: 150, customRender: ({ record }) => `${record.commission} ${record.commission_asset || '资产未知'}` },
  { title: '订单 / 成交', key: 'ids', width: 220, customRender: ({ record }) => `${record.order_id} / ${record.trade_id}` }
]

function formatDuration(value: number | null): string {
  if (value == null) return '—'
  const seconds = Math.max(0, Math.round(value / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

function failureMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

async function load() {
  loading.value = true
  error.value = null
  campaignPnl.value = null
  campaignPnlError.value = null
  fills.value = []
  fillError.value = null
  events.value = []
  eventError.value = null
  if (!routeReady.value) {
    error.value = 'Campaign 链接缺少账户、策略或交易对身份。'
    loading.value = false
    return
  }
  const [pnlResult, eventResult, fillResult] = await Promise.allSettled([
    operationsApi.campaignPnl(campaignId.value, { account_id: accountId.value, strategy_id: strategyId.value }),
    operationsApi.strategyAuditEvents({ account_id: accountId.value, strategy_id: strategyId.value, campaign_id: campaignId.value, limit: 200 }),
    collectPageItems((page) => operationsApi.trades({ account_id: accountId.value, strategy_id: strategyId.value, symbol: symbol.value, campaign_id: campaignId.value, ...page }))
  ])
  campaignPnl.value = pnlResult.status === 'fulfilled' ? pnlResult.value : null
  campaignPnlError.value = pnlResult.status === 'rejected' ? failureMessage(pnlResult.reason, 'Campaign PnL 事实不完整') : null
  events.value = eventResult.status === 'fulfilled' ? eventResult.value.items : []
  eventError.value = eventResult.status === 'rejected' ? failureMessage(eventResult.reason, '策略审计事件读取失败') : null
  fills.value = fillResult.status === 'fulfilled' ? fillResult.value.items : []
  fillError.value = fillResult.status === 'rejected' ? failureMessage(fillResult.reason, '账本成交读取失败') : null
  if (pnlResult.status === 'rejected' && eventResult.status === 'rejected' && fillResult.status === 'rejected') {
    error.value = 'Campaign 明细读取失败'
  }
  refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  loading.value = false
}

function backToTrades() {
  void router.push({
    name: 'trades',
    query: {
      account_id: accountId.value,
      strategy_id: strategyId.value,
      symbol: symbol.value
    }
  })
}

watch(routeKey, () => { void load() }, { immediate: true })
</script>

<template>
  <main class="operations-page campaign-detail-page">
    <PageHeader eyebrow="ANALYSIS / EXECUTION TRACE" :title="symbol ? `${symbol} Campaign 成交` : 'Campaign 成交'" description="查看完整账本成交、K 线买卖点与策略审计事件。" :loading="loading" :refreshed-at="refreshedAt" @refresh="load">
      <template #actions>
        <a-tooltip title="返回成交复盘"><a-button type="text" shape="circle" aria-label="返回成交复盘" @click="backToTrades"><ArrowLeft :size="16" /></a-button></a-tooltip>
      </template>
    </PageHeader>

    <DataState :loading="loading" :error="error" @retry="load">
      <section class="campaign-identity">
        <div><span>CAMPAIGN</span><strong>{{ campaignId || '—' }}</strong></div>
        <div><span>SYMBOL</span><strong>{{ symbol || '—' }}</strong></div>
        <div><span>ACCOUNT / STRATEGY</span><strong>{{ accountId || '—' }} / {{ strategyId || '—' }}</strong></div>
      </section>

      <section v-if="campaignPnl" class="metric-grid campaign-detail-metrics">
        <article><span>{{ campaignPnl.has_open_quantity ? '当前净已实现 PnL' : '净已实现 PnL' }}</span><strong :class="pnlClass(campaignPnl.net_realized_pnl)">{{ formatMoney(campaignPnl.net_realized_pnl) }}</strong></article>
        <article><span>毛已实现 PnL</span><strong>{{ formatMoney(campaignPnl.gross_realized_pnl) }}</strong></article>
        <article><span>总手续费</span><strong>{{ formatMoney(campaignPnl.total_commission) }} {{ campaignPnl.commission_asset || '资产未知' }}</strong></article>
        <article><span>剩余数量</span><strong>{{ campaignPnl.remaining_quantity }}</strong></article>
        <article><span>状态</span><strong>{{ campaignPnl.closed_at ? '已结束' : campaignPnl.has_open_quantity ? '仍有敞口' : '事实不完整' }}</strong></article>
        <article><span>卖出 / 买入均价</span><strong>{{ formatMoney(campaignPnl.sell_avg_price, 6) }} / {{ formatMoney(campaignPnl.buy_avg_price, 6) }}</strong></article>
        <article><span>闭合时间</span><strong>{{ formatDateTime(campaignPnl.closed_at) }}</strong></article>
        <article><span>生命周期</span><strong>{{ formatDuration(campaignPnl.lifecycle_duration_ms) }}</strong></article>
      </section>
      <a-alert v-if="campaignPnl" type="info" show-icon message="资金费、滑点与规范化退出原因暂不可用" :description="campaignPnl.has_open_quantity ? '该 Campaign 尚有敞口；当前数值是截至最新 fill 的账本已实现 PnL 扣 USDT 手续费，不是最终轮次收益。' : '该 Campaign 已完整闭合；净 PnL 为账本 realized_pnl 扣 USDT 手续费。'" />
      <a-alert v-else-if="campaignPnlError" type="warning" show-icon message="Campaign 净 PnL 不可用" :description="`${campaignPnlError}；下方仍展示权威账本成交。`" />

      <TradeReplayChartPanel v-if="campaignChartTrade" :key="routeKey" :trade="campaignChartTrade" mode="market" fill-display="all" fill-time-semantics="exchange" exit-label="最后成交" :strategy-lines="false" />
      <a-alert v-else-if="!fillError" class="campaign-chart-empty" type="info" show-icon message="暂无可定位到 K 线的账本成交" />

      <section class="detail-section"><h3>全部账本成交</h3>
        <a-alert v-if="fillError" type="warning" show-icon :message="fillError" class="section-alert" />
        <a-table v-else :columns="fillColumns" :data-source="sortedFills" row-key="id" size="small" :pagination="false" :scroll="{ x: 1080 }" />
      </section>
      <section class="detail-section campaign-events"><h3>策略事件时间线</h3>
        <a-alert v-if="eventError" type="warning" show-icon :message="eventError" class="section-alert" />
        <a-timeline v-else-if="events.length">
          <a-timeline-item v-for="event in [...events].sort((a, b) => a.event_time - b.event_time)" :key="event.id">
            <div class="event-title"><strong>{{ event.event_type }}</strong><time>{{ formatDateTime(event.event_time) }}</time></div>
            <p>{{ event.symbol }} · {{ event.event_key }}</p>
            <pre v-if="Object.keys(event.details).length">{{ JSON.stringify(event.details, null, 2) }}</pre>
          </a-timeline-item>
        </a-timeline>
        <div v-else class="timeline-empty"><CircleDotDashed :size="16" /> 当前 Campaign 没有可查询的策略审计事件</div>
      </section>
    </DataState>
  </main>
</template>

<style scoped lang="scss">
.campaign-identity { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-bottom:12px; }
.campaign-identity > div,.campaign-detail-metrics article { padding:10px; border:1px solid var(--line); border-radius:5px; background:var(--surface); }
.campaign-identity span,.campaign-detail-metrics span { display:block; color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }
.campaign-identity strong,.campaign-detail-metrics strong { display:block; margin-top:5px; overflow-wrap:anywhere; font:var(--font-size-sm) var(--font-family-mono); }
.campaign-detail-metrics { margin-bottom:14px; }.detail-section { margin-top:18px; }.detail-section h3 { margin:0 0 9px; font-size:var(--font-size-md); }
.section-alert,.campaign-chart-empty { margin-top:14px; }.campaign-events p { color:var(--muted); font-size:var(--font-size-xs); }.event-title { display:flex; justify-content:space-between; gap:10px; }.event-title time { color:var(--muted); font-size:var(--font-size-xs); }.campaign-events pre { max-height:180px; margin:6px 0 0; padding:8px; overflow:auto; border:1px solid var(--line); background:var(--surface-hover); color:var(--text); font:var(--font-size-xs) var(--font-family-mono); white-space:pre-wrap; }.timeline-empty { display:flex; gap:7px; align-items:center; color:var(--muted); font-size:var(--font-size-xs); }
@media(max-width:700px){.campaign-identity{grid-template-columns:1fr}}
</style>
