<script setup lang="ts">
import { computed, h, onActivated, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Tag, type TableColumnsType, type TablePaginationConfig } from 'ant-design-vue'
import { CandlestickChart, CircleDotDashed } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { CampaignPnL, CampaignSummary, LedgerTrade, StrategyAuditEvent } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { formatDateTime, formatMoney, pnlClass, sideLabel } from '@/features/operations/format'
import { collectPageItems } from '@/features/operations/pagination'

function positiveInt(value: unknown, fallback: number, max = Number.MAX_SAFE_INTEGER): number {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? Math.min(parsed, max) : fallback
}

function campaignKey(item: CampaignSummary): string {
  return `${item.account_id}:${item.strategy_id}:${item.campaign_id}`
}

const route = useRoute()
const router = useRouter()
const filters = ref<OperationFilters>({
  account_id: String(route.query.account_id ?? ''),
  strategy_id: String(route.query.strategy_id ?? ''),
  symbol: String(route.query.symbol ?? '')
})
const selectedDate = ref(String(route.query.date ?? ''))
const currentPage = ref(positiveInt(route.query.page, 1))
const pageSize = ref(positiveInt(route.query.page_size, 50, 1000))
const campaigns = ref<CampaignSummary[]>([])
const serverTotal = ref(0)
const unattributedFills = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)
const detailOpen = ref(false)
const detailLoading = ref(false)
const selected = ref<CampaignSummary | null>(null)
const campaignPnl = ref<CampaignPnL | null>(null)
const campaignPnlError = ref<string | null>(null)
const detailFills = ref<LedgerTrade[]>([])
const events = ref<StrategyAuditEvent[]>([])
let initialActivation = true

const query = computed(() => ({
  ...(filters.value.account_id.trim() ? { account_id: filters.value.account_id.trim() } : {}),
  ...(filters.value.strategy_id.trim() ? { strategy_id: filters.value.strategy_id.trim() } : {}),
  ...(filters.value.symbol.trim() ? { symbol: filters.value.symbol.trim() } : {})
}))
const requestedCampaign = computed(() => String(route.query.campaign_id ?? ''))
const tablePagination = computed<TablePaginationConfig>(() => ({
  current: currentPage.value,
  pageSize: pageSize.value,
  total: serverTotal.value,
  showSizeChanger: true,
  pageSizeOptions: ['25', '50', '100', '250'],
  showTotal: (total) => `共 ${total} Campaigns`
}))

const campaignColumns: TableColumnsType<CampaignSummary> = [
  { title: '交易对', dataIndex: 'symbol', key: 'symbol', fixed: 'left', width: 132, customRender: ({ text }) => h('strong', { class: 'campaign-symbol' }, String(text)) },
  { title: 'Campaign', dataIndex: 'campaign_id', key: 'campaign', width: 270, ellipsis: true, customRender: ({ record }) => h(Button, { type: 'link', class: 'campaign-button', onClick: () => openCampaign(record) }, () => record.campaign_id) },
  { title: '状态', key: 'status', width: 108, customRender: ({ record }) => h(Tag, { class: 'campaign-status', color: record.closed_at ? 'success' : record.has_open_quantity ? 'processing' : 'warning' }, () => record.closed_at ? '已结束' : record.has_open_quantity ? '进行中' : '事实不完整') },
  { title: 'fills', dataIndex: 'fill_count', key: 'fills', width: 75 },
  { title: '净已实现 PnL', dataIndex: 'net_realized_pnl', key: 'netPnl', width: 135, customCell: (record) => ({ class: record.net_realized_pnl == null ? 'value-unavailable' : pnlClass(record.net_realized_pnl) }), customRender: ({ record }) => record.net_realized_pnl == null ? h('span', { class: 'net-unavailable', title: record.commission_asset === 'USDT' ? 'Campaign 尚未闭合或 PnL 事实不完整' : '手续费缺少权威 USDT 换算' }, '不可用') : formatMoney(record.net_realized_pnl) },
  { title: '毛 PnL', dataIndex: 'gross_realized_pnl', key: 'grossPnl', width: 115, customRender: ({ text }) => formatMoney(String(text)) },
  { title: '手续费', key: 'commission', width: 150, customRender: ({ record }) => `${formatMoney(record.total_commission)} ${record.commission_asset || '资产不一致'}` },
  { title: '账户', dataIndex: 'account_id', key: 'account', width: 145 },
  { title: '策略', dataIndex: 'strategy_id', key: 'strategy', width: 145 },
  { title: '首笔成交', dataIndex: 'first_fill_at', key: 'first', width: 185, customRender: ({ text }) => formatDateTime(String(text)) },
  { title: '闭合 / 末笔时间', key: 'closed', width: 185, customRender: ({ record }) => formatDateTime(record.closed_at || record.last_fill_at) }
]

const fillColumns: TableColumnsType<LedgerTrade> = [
  { title: '时间', dataIndex: 'exchange_time', key: 'time', width: 180, customRender: ({ text }) => formatDateTime(String(text)) },
  { title: '方向', dataIndex: 'side', key: 'side', width: 105, customRender: ({ text }) => sideLabel(String(text)) },
  { title: '价格', dataIndex: 'price', key: 'price', width: 130, customRender: ({ text }) => formatMoney(String(text), 6) },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 120 },
  { title: '已实现 PnL', dataIndex: 'realized_pnl', key: 'pnl', width: 125, customCell: (record) => ({ class: pnlClass(record.realized_pnl) }), customRender: ({ text }) => formatMoney(text == null ? null : String(text)) },
  { title: '手续费', key: 'fee', width: 150, customRender: ({ record }) => `${record.commission} ${record.commission_asset || '资产未知'}` },
  { title: '订单 / 成交', key: 'ids', width: 220, customRender: ({ record }) => `${record.order_id} / ${record.trade_id}` }
]

async function load() {
  loading.value = true
  error.value = null
  try {
    const page = await operationsApi.campaigns({
      ...query.value,
      ...(requestedCampaign.value ? { campaign_id: requestedCampaign.value } : {}),
      ...(selectedDate.value ? { start_date: selectedDate.value, end_date: selectedDate.value, timezone: 'Asia/Shanghai' as const } : {}),
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value
    })
    serverTotal.value = page.total
    const lastPage = Math.max(1, Math.ceil(page.total / pageSize.value))
    if (currentPage.value > lastPage) {
      currentPage.value = lastPage
      await syncRoute()
      await load()
      return
    }
    campaigns.value = page.items
    unattributedFills.value = page.unattributed_fills
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    const requested = requestedCampaign.value ? campaigns.value.find((item) => item.campaign_id === requestedCampaign.value) : null
    if (requested) await openCampaign(requested)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '成交复盘加载失败'
  } finally {
    loading.value = false
  }
}

async function applyFilters() {
  currentPage.value = 1
  await syncRoute()
  await load()
}

async function syncRoute() {
  await router.replace({ query: {
    ...query.value,
    ...(selectedDate.value ? { date: selectedDate.value } : {}),
    ...(requestedCampaign.value ? { campaign_id: requestedCampaign.value } : {}),
    page: String(currentPage.value),
    page_size: String(pageSize.value)
  } })
}

async function changePage(pagination: TablePaginationConfig) {
  const nextSize = positiveInt(pagination.pageSize, pageSize.value, 1000)
  currentPage.value = nextSize === pageSize.value
    ? positiveInt(pagination.current, currentPage.value)
    : 1
  pageSize.value = nextSize
  await syncRoute()
  await load()
}

async function openCampaign(group: CampaignSummary) {
  selected.value = group
  campaignPnl.value = null
  campaignPnlError.value = null
  detailFills.value = []
  events.value = []
  detailOpen.value = true
  detailLoading.value = true
  const [pnlResult, eventResult, fillResult] = await Promise.allSettled([
    operationsApi.campaignPnl(group.campaign_id, { account_id: group.account_id, strategy_id: group.strategy_id }),
    operationsApi.strategyAuditEvents({ account_id: group.account_id, strategy_id: group.strategy_id, campaign_id: group.campaign_id, limit: 200 }),
    collectPageItems((page) => operationsApi.trades({ account_id: group.account_id, strategy_id: group.strategy_id, symbol: group.symbol, campaign_id: group.campaign_id, ...page }))
  ])
  campaignPnl.value = pnlResult.status === 'fulfilled' ? pnlResult.value : null
  campaignPnlError.value = pnlResult.status === 'rejected'
    ? (pnlResult.reason instanceof Error ? pnlResult.reason.message : 'Campaign PnL 事实不完整')
    : null
  events.value = eventResult.status === 'fulfilled' ? eventResult.value.items : []
  detailFills.value = fillResult.status === 'fulfilled' ? fillResult.value.items : []
  detailLoading.value = false
}

function formatDuration(value: number | null): string {
  if (value == null) return '—'
  const seconds = Math.max(0, Math.round(value / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

onMounted(load)
onActivated(() => {
  if (initialActivation) {
    initialActivation = false
    return
  }
  filters.value = {
    account_id: String(route.query.account_id ?? ''),
    strategy_id: String(route.query.strategy_id ?? ''),
    symbol: String(route.query.symbol ?? '')
  }
  selectedDate.value = String(route.query.date ?? '')
  currentPage.value = positiveInt(route.query.page, 1)
  pageSize.value = positiveInt(route.query.page_size, 50, 1000)
  void load()
})
</script>

<template>
  <main class="operations-page trade-review-page">
    <PageHeader eyebrow="ANALYSIS / EXECUTION TRACE" title="成交复盘" description="以服务端完整 Campaign 为浏览单位，下钻到全部账本 fills 和策略审计事件。" :loading="loading" :refreshed-at="refreshedAt" @refresh="load" />
    <FilterBar v-model="filters" @reset="selectedDate = ''" @apply="applyFilters">
      <template #extra-fields>
        <label class="trade-date-filter"><span>成交日期</span><a-date-picker :value="selectedDate || undefined" value-format="YYYY-MM-DD" allow-clear placeholder="全部日期" @update:value="selectedDate = String($event ?? '')" /></label>
      </template>
    </FilterBar>
    <div class="review-tools">
      <span>服务端共 {{ serverTotal }} Campaigns · 当前页 {{ campaigns.length }} 个 · 未归属 {{ unattributedFills }} fills</span>
    </div>
    <a-alert v-if="unattributedFills" type="warning" show-icon :message="`${unattributedFills} 笔成交缺少 Campaign 归属`" description="未归属 fills 不参与 Campaign 列表、收益日历或绩效统计，页面不会按时间相邻关系猜测归属。" class="scope-alert" />
    <a-alert v-else type="info" show-icon message="Campaign 由服务端完整聚合" description="分页单位为完整 Campaign；非 USDT 手续费没有权威换算时，净 PnL 明确显示不可用。结果与规范化退出原因筛选仍待权威字段。" class="scope-alert" />
    <DataState :loading="loading" :error="error" :empty="!campaigns.length" @retry="load">
      <div class="table-frame"><a-table :columns="campaignColumns" :data-source="campaigns" :row-key="campaignKey" :pagination="tablePagination" :scroll="{ x: 1670 }" @change="changePage" /></div>
    </DataState>

    <a-drawer v-model:open="detailOpen" width="min(860px, 96vw)" title="Campaign 成交链路">
      <a-spin :spinning="detailLoading">
        <template v-if="selected">
          <div class="campaign-identity"><div><span>CAMPAIGN</span><strong>{{ selected.campaign_id }}</strong></div><div><span>SYMBOL</span><strong>{{ selected.symbol }}</strong></div><div><span>ACCOUNT / STRATEGY</span><strong>{{ selected.account_id }} / {{ selected.strategy_id }}</strong></div></div>
          <div v-if="campaignPnl" class="metric-grid detail-metrics">
            <article><span>{{ campaignPnl.has_open_quantity ? '当前净已实现 PnL' : '净已实现 PnL' }}</span><strong :class="pnlClass(campaignPnl.net_realized_pnl)">{{ formatMoney(campaignPnl.net_realized_pnl) }}</strong></article>
            <article><span>毛已实现 PnL</span><strong>{{ formatMoney(campaignPnl.gross_realized_pnl) }}</strong></article>
            <article><span>总手续费</span><strong>{{ formatMoney(campaignPnl.total_commission) }} {{ campaignPnl.commission_asset || '资产未知' }}</strong></article>
            <article><span>剩余数量</span><strong>{{ campaignPnl.remaining_quantity }}</strong></article>
            <article><span>状态</span><strong>{{ campaignPnl.closed_at ? '已结束' : campaignPnl.has_open_quantity ? '仍有敞口' : '事实不完整' }}</strong></article>
            <article><span>卖出 / 买回均价</span><strong>{{ formatMoney(campaignPnl.sell_avg_price, 6) }} / {{ formatMoney(campaignPnl.buy_avg_price, 6) }}</strong></article>
            <article><span>闭合时间</span><strong>{{ formatDateTime(campaignPnl.closed_at) }}</strong></article>
            <article><span>生命周期</span><strong>{{ formatDuration(campaignPnl.lifecycle_duration_ms) }}</strong></article>
          </div>
          <a-alert v-if="campaignPnl" type="info" show-icon message="资金费、滑点与规范化退出原因暂不可用" :description="campaignPnl.has_open_quantity ? '该 Campaign 尚有敞口；当前数值是截至最新 fill 的账本已实现 PnL 扣 USDT 手续费，不是最终轮次收益。' : '该 Campaign 已完整闭合；净 PnL 为账本 realized_pnl 扣 USDT 手续费。'" />
          <a-alert v-else-if="!detailLoading" type="warning" show-icon message="Campaign 净 PnL 不可用" :description="`${campaignPnlError || 'Campaign PnL 事实不完整'}；下方仍展示权威账本成交。`" />

          <section class="drawer-section"><h3>全部账本成交</h3><a-table :columns="fillColumns" :data-source="detailFills" row-key="id" size="small" :pagination="false" :scroll="{ x: 1080 }" /></section>
          <section class="drawer-section chart-unavailable"><CandlestickChart :size="24" /><div><h3>K 线买卖点</h3><p>实盘账本尚未提供按 Campaign 查询 K 线的稳定接口；不使用回测数据或联网行情冒充实盘复盘。</p></div></section>
          <section class="drawer-section"><h3>策略事件时间线</h3>
            <a-timeline v-if="events.length">
              <a-timeline-item v-for="event in [...events].sort((a, b) => a.event_time - b.event_time)" :key="event.id"><div class="event-title"><strong>{{ event.event_type }}</strong><time>{{ formatDateTime(event.event_time) }}</time></div><p>{{ event.symbol }} · {{ event.event_key }}</p><pre v-if="Object.keys(event.details).length">{{ JSON.stringify(event.details, null, 2) }}</pre></a-timeline-item>
            </a-timeline>
            <div v-else class="timeline-empty"><CircleDotDashed :size="16" /> 当前 Campaign 没有可查询的策略审计事件</div>
          </section>
        </template>
      </a-spin>
    </a-drawer>
  </main>
</template>

<style scoped lang="scss">
.review-tools { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }.scope-alert { margin-bottom:12px; }.campaign-symbol,.campaign-button { display:block; width:100%; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.campaign-button { padding-inline:0; text-align:left; }.campaign-status { white-space:nowrap; }.net-unavailable,.value-unavailable { color:var(--color-warning); text-decoration:underline dotted; text-underline-offset:3px; }.campaign-identity { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-bottom:12px; }.campaign-identity > div,.detail-metrics article { padding:10px; border:1px solid var(--line); border-radius:5px; background:var(--surface); }.campaign-identity span,.detail-metrics span { display:block; color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }.campaign-identity strong,.detail-metrics strong { display:block; margin-top:5px; overflow-wrap:anywhere; font:var(--font-size-sm) var(--font-family-mono); }.detail-metrics { margin-bottom:14px; }.drawer-section { margin-top:18px; }.drawer-section h3 { margin:0 0 9px; font-size:var(--font-size-md); }.chart-unavailable { display:flex; align-items:center; gap:12px; padding:14px; border:1px dashed var(--line); color:var(--muted); }.chart-unavailable h3 { color:var(--text); }.chart-unavailable p { margin:0; font-size:var(--font-size-xs); }.event-title { display:flex; justify-content:space-between; gap:10px; }.event-title time { color:var(--muted); font-size:var(--font-size-xs); }.drawer-section p { color:var(--muted); font-size:var(--font-size-xs); }.drawer-section pre { max-height:180px; margin:6px 0 0; padding:8px; overflow:auto; border:1px solid var(--line); background:var(--surface-hover); color:var(--text); font:var(--font-size-xs) var(--font-family-mono); white-space:pre-wrap; }.timeline-empty { display:flex; gap:7px; align-items:center; color:var(--muted); font-size:var(--font-size-xs); }
@media(max-width:700px){.campaign-identity{grid-template-columns:1fr}.review-tools{align-items:flex-start;flex-direction:column}}
</style>
