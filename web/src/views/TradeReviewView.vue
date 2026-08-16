<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, type TableColumnsType } from 'ant-design-vue'
import { CandlestickChart, CircleDotDashed } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { CampaignPnL, LedgerTrade, StrategyAuditEvent } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { asNumber, formatDateTime, formatMoney, pnlClass, sideLabel } from '@/features/operations/format'

interface CampaignGroup {
  key: string
  campaignId: string | null
  accountId: string
  strategyId: string
  symbol: string
  fills: LedgerTrade[]
  firstTime: string
  lastTime: string
  realizedPnl: number
  commission: number
}

const route = useRoute()
const router = useRouter()
const filters = ref<OperationFilters>({
  account_id: String(route.query.account_id ?? ''),
  strategy_id: String(route.query.strategy_id ?? ''),
  symbol: String(route.query.symbol ?? '')
})
const selectedDate = ref(String(route.query.date ?? ''))
const requestedCampaign = String(route.query.campaign_id ?? '')
const trades = ref<LedgerTrade[]>([])
const serverTotal = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)
const detailOpen = ref(false)
const detailLoading = ref(false)
const selected = ref<CampaignGroup | null>(null)
const campaignPnl = ref<CampaignPnL | null>(null)
const events = ref<StrategyAuditEvent[]>([])

const query = computed(() => ({
  ...(filters.value.account_id.trim() ? { account_id: filters.value.account_id.trim() } : {}),
  ...(filters.value.strategy_id.trim() ? { strategy_id: filters.value.strategy_id.trim() } : {}),
  ...(filters.value.symbol.trim() ? { symbol: filters.value.symbol.trim() } : {})
}))

function shanghaiDate(value: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value))
  const record = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${record.year}-${record.month}-${record.day}`
}

const visibleTrades = computed(() => selectedDate.value ? trades.value.filter((trade) => shanghaiDate(trade.exchange_time) === selectedDate.value) : trades.value)
const campaigns = computed<CampaignGroup[]>(() => {
  const groups = new Map<string, LedgerTrade[]>()
  for (const trade of visibleTrades.value) {
    const key = trade.campaign_id ? `${trade.account_id}:${trade.strategy_id}:${trade.campaign_id}` : `unattributed:${trade.id}`
    groups.set(key, [...(groups.get(key) ?? []), trade])
  }
  return [...groups.entries()].map(([key, fills]) => {
    const ordered = [...fills].sort((a, b) => a.exchange_time.localeCompare(b.exchange_time))
    const first = ordered[0]!
    const last = ordered[ordered.length - 1]!
    return {
      key,
      campaignId: first.campaign_id,
      accountId: first.account_id,
      strategyId: first.strategy_id,
      symbol: first.symbol,
      fills: ordered,
      firstTime: first.exchange_time,
      lastTime: last.exchange_time,
      realizedPnl: fills.reduce((sum, item) => sum + asNumber(item.realized_pnl), 0),
      commission: fills.reduce((sum, item) => sum + asNumber(item.commission), 0)
    }
  }).sort((a, b) => b.lastTime.localeCompare(a.lastTime))
})

const campaignColumns: TableColumnsType<CampaignGroup> = [
  { title: 'Campaign / 交易对', key: 'campaign', fixed: 'left', width: 235, customRender: ({ record }) => h(Button, { type: 'link', class: 'campaign-button', onClick: () => openCampaign(record) }, () => record.campaignId ? `${record.symbol} · ${record.campaignId}` : `${record.symbol} · 未归属成交`) },
  { title: '成交数', key: 'fills', width: 90, customRender: ({ record }) => record.fills.length },
  { title: '已实现 PnL', dataIndex: 'realizedPnl', key: 'realizedPnl', width: 130, customCell: (record) => ({ class: pnlClass(record.realizedPnl) }), customRender: ({ text }) => formatMoney(Number(text)) },
  { title: '手续费', dataIndex: 'commission', key: 'commission', width: 115, customRender: ({ text }) => formatMoney(Number(text)) },
  { title: '账户', dataIndex: 'accountId', key: 'account', width: 145 },
  { title: '策略', dataIndex: 'strategyId', key: 'strategy', width: 145 },
  { title: '首笔成交', dataIndex: 'firstTime', key: 'first', width: 185, customRender: ({ text }) => formatDateTime(String(text)) },
  { title: '末笔成交', dataIndex: 'lastTime', key: 'last', width: 185, customRender: ({ text }) => formatDateTime(String(text)) }
]

const fillColumns: TableColumnsType<LedgerTrade> = [
  { title: '时间', dataIndex: 'exchange_time', key: 'time', width: 180, customRender: ({ text }) => formatDateTime(String(text)) },
  { title: '方向', dataIndex: 'side', key: 'side', width: 105, customRender: ({ text }) => sideLabel(String(text)) },
  { title: '价格', dataIndex: 'price', key: 'price', width: 130, customRender: ({ text }) => formatMoney(String(text), 6) },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 120 },
  { title: '已实现 PnL', dataIndex: 'realized_pnl', key: 'pnl', width: 125, customCell: (record) => ({ class: pnlClass(record.realized_pnl) }), customRender: ({ text }) => formatMoney(text == null ? null : String(text)) },
  { title: '手续费', dataIndex: 'commission', key: 'fee', width: 110 },
  { title: '订单 / 成交', key: 'ids', width: 220, customRender: ({ record }) => `${record.order_id} / ${record.trade_id}` }
]

async function load() {
  loading.value = true
  error.value = null
  try {
    const page = await operationsApi.trades({ ...query.value, limit: 1000 })
    trades.value = page.items
    serverTotal.value = page.total
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    const requested = requestedCampaign ? campaigns.value.find((item) => item.campaignId === requestedCampaign) : null
    if (requested) await openCampaign(requested)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '成交复盘加载失败'
  } finally {
    loading.value = false
  }
}

async function applyFilters() {
  await router.replace({ query: { ...query.value, ...(selectedDate.value ? { date: selectedDate.value } : {}) } })
  await load()
}

async function openCampaign(group: CampaignGroup) {
  selected.value = group
  campaignPnl.value = null
  events.value = []
  detailOpen.value = true
  if (!group.campaignId) return
  detailLoading.value = true
  const [pnlResult, eventResult] = await Promise.allSettled([
    operationsApi.campaignPnl(group.campaignId, { account_id: group.accountId, strategy_id: group.strategyId }),
    operationsApi.strategyAuditEvents({ account_id: group.accountId, strategy_id: group.strategyId, campaign_id: group.campaignId, limit: 200 })
  ])
  campaignPnl.value = pnlResult.status === 'fulfilled' ? pnlResult.value : null
  events.value = eventResult.status === 'fulfilled' ? eventResult.value.items : []
  detailLoading.value = false
}

onMounted(load)
</script>

<template>
  <main class="operations-page trade-review-page">
    <PageHeader eyebrow="ANALYSIS / EXECUTION TRACE" title="成交复盘" description="以 Campaign 为主要浏览单位，下钻到账本 fills 和策略审计事件；未归属 Campaign 的成交单独标识。" :loading="loading" :refreshed-at="refreshedAt" @refresh="load" />
    <FilterBar v-model="filters" @apply="applyFilters" />
    <div class="review-tools">
      <label><span>上海自然日</span><a-date-picker :value="selectedDate || undefined" value-format="YYYY-MM-DD" allow-clear @update:value="selectedDate = String($event ?? '')" @change="applyFilters" /></label>
      <span>服务端共 {{ serverTotal }} 笔；当前最多载入 1000 笔并聚合为 {{ campaigns.length }} 个浏览单元</span>
    </div>
    <a-alert type="info" show-icon message="结果和退出原因筛选尚不可用" description="现有成交查询没有退出原因，Campaign 列表接口也尚未提供；页面不会从单笔 fill 猜测退出原因。" class="scope-alert" />
    <DataState :loading="loading" :error="error" :empty="!campaigns.length" @retry="load">
      <div class="table-frame"><a-table :columns="campaignColumns" :data-source="campaigns" row-key="key" :pagination="{ pageSize: 25, showSizeChanger: true }" :scroll="{ x: 1220 }" /></div>
    </DataState>

    <a-drawer v-model:open="detailOpen" width="min(860px, 96vw)" title="Campaign 成交链路">
      <a-spin :spinning="detailLoading">
        <template v-if="selected">
          <div class="campaign-identity"><div><span>CAMPAIGN</span><strong>{{ selected.campaignId || '未归属' }}</strong></div><div><span>SYMBOL</span><strong>{{ selected.symbol }}</strong></div><div><span>ACCOUNT / STRATEGY</span><strong>{{ selected.accountId }} / {{ selected.strategyId }}</strong></div></div>
          <div v-if="campaignPnl" class="metric-grid detail-metrics">
            <article><span>净已实现 PnL</span><strong :class="pnlClass(campaignPnl.net_realized_pnl)">{{ formatMoney(campaignPnl.net_realized_pnl) }}</strong></article>
            <article><span>总手续费</span><strong>{{ formatMoney(campaignPnl.total_commission) }}</strong></article>
            <article><span>剩余数量</span><strong>{{ campaignPnl.remaining_quantity }}</strong></article>
            <article><span>状态</span><strong>{{ campaignPnl.has_open_quantity ? '仍有敞口' : '已结束' }}</strong></article>
          </div>
          <a-alert v-else-if="selected.campaignId && !detailLoading" type="warning" show-icon message="Campaign PnL 无法读取；下方仍展示账本成交事实。" />

          <section class="drawer-section"><h3>底层成交</h3><a-table :columns="fillColumns" :data-source="selected.fills" row-key="id" size="small" :pagination="false" :scroll="{ x: 1040 }" /></section>
          <section class="drawer-section chart-unavailable"><CandlestickChart :size="24" /><div><h3>K 线买卖点</h3><p>实盘账本尚未提供按 Campaign 查询 K 线的稳定接口；不使用回测数据或联网行情冒充实盘复盘。</p></div></section>
          <section class="drawer-section"><h3>策略事件时间线</h3>
            <a-timeline v-if="events.length">
              <a-timeline-item v-for="event in events" :key="event.id"><div class="event-title"><strong>{{ event.event_type }}</strong><time>{{ formatDateTime(event.created_at) }}</time></div><p>{{ event.symbol }} · {{ event.event_key }}</p></a-timeline-item>
            </a-timeline>
            <div v-else class="timeline-empty"><CircleDotDashed :size="16" /> 当前 Campaign 没有可查询的策略审计事件</div>
          </section>
        </template>
      </a-spin>
    </a-drawer>
  </main>
</template>

<style scoped lang="scss">
.review-tools { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; margin-bottom:10px; color:var(--muted); font:10px "IBM Plex Mono",monospace; }.review-tools label { display:grid; gap:4px; }.scope-alert { margin-bottom:12px; }.campaign-button { display:block; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.campaign-identity { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-bottom:12px; }.campaign-identity > div,.detail-metrics article { padding:10px; border:1px solid var(--line); border-radius:5px; background:var(--surface); }.campaign-identity span,.detail-metrics span { display:block; color:var(--muted); font:9px "IBM Plex Mono",monospace; }.campaign-identity strong,.detail-metrics strong { display:block; margin-top:5px; overflow-wrap:anywhere; font:12px "IBM Plex Mono",monospace; }.detail-metrics { margin-bottom:14px; }.drawer-section { margin-top:18px; }.drawer-section h3 { margin:0 0 9px; font-size:13px; }.chart-unavailable { display:flex; align-items:center; gap:12px; padding:14px; border:1px dashed var(--line); color:var(--muted); }.chart-unavailable h3 { color:var(--text); }.chart-unavailable p { margin:0; font-size:11px; }.event-title { display:flex; justify-content:space-between; gap:10px; }.event-title time { color:var(--muted); font-size:10px; }.drawer-section p { color:var(--muted); font-size:11px; }.timeline-empty { display:flex; gap:7px; align-items:center; color:var(--muted); font-size:11px; }
@media(max-width:700px){.campaign-identity{grid-template-columns:1fr}.review-tools{align-items:flex-start;flex-direction:column}}
</style>
