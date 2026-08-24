<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Tag, type TableColumnsType, type TablePaginationConfig } from 'ant-design-vue'
import { operationsApi } from '@/api/operations'
import type { CampaignSummary } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import { campaignRoute } from '@/features/operations/campaignRoute'
import FilterBar from '@/features/operations/FilterBar.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import {
  isQuerySynced,
  useLedgerLoader,
  useOperationFilters,
  usePageParams,
  useQuerySync
} from '@/features/operations/useOperationsView'
import { formatDateTime, formatMoney, pnlClass } from '@/shared/format'
import { LEDGER_TIMEZONE } from '@/shared/time'

/** Campaign 的账本身份是三段复合键，单独的 campaign_id 并不唯一。 */
function campaignKey(item: CampaignSummary): string {
  return `${item.account_id}:${item.strategy_id}:${item.campaign_id}`
}

const route = useRoute()
const router = useRouter()
const syncQuery = useQuerySync()
const { filters, query, restore: restoreFilters } = useOperationFilters()
const { page, pageSize, offset, restore: restorePage, apply: applyPage } = usePageParams({ defaultSize: 50, maxSize: 1000 })
const selectedDate = ref(String(route.query.date ?? ''))
const campaigns = ref<CampaignSummary[]>([])
const serverTotal = ref(0)
const unattributedFills = ref(0)

const requestedCampaign = computed(() => String(route.query.campaign_id ?? ''))
const tablePagination = computed<TablePaginationConfig>(() => ({
  current: page.value,
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

/** 本页放进地址栏的内容。写回与「URL 是否被外部改动」共用这一处声明。 */
function routeQuery() {
  return {
    ...query.value,
    ...(selectedDate.value ? { date: selectedDate.value } : {}),
    ...(requestedCampaign.value ? { campaign_id: requestedCampaign.value } : {}),
    page: page.value,
    page_size: pageSize.value
  }
}

/** 把地址栏状态同步回本地 ref。 */
function restoreFromRoute() {
  restoreFilters()
  restorePage()
  selectedDate.value = String(route.query.date ?? '')
}

const { loading, error, refreshedAt, reload } = useLedgerLoader(async ({ isStale }) => {
  const result = await operationsApi.campaigns({
    ...query.value,
    ...(requestedCampaign.value ? { campaign_id: requestedCampaign.value } : {}),
    ...(selectedDate.value ? { start_date: selectedDate.value, end_date: selectedDate.value, timezone: LEDGER_TIMEZONE } : {}),
    limit: pageSize.value,
    offset: offset.value
  })
  if (isStale()) return
  serverTotal.value = result.total

  // 删除记录后页码可能越界，收敛到最后一页再取一次，而不是显示空表。
  const lastPage = Math.max(1, Math.ceil(result.total / pageSize.value))
  if (page.value > lastPage) {
    page.value = lastPage
    await syncRoute()
    await reload()
    return
  }
  campaigns.value = result.items
  unattributedFills.value = result.unattributed_fills
}, {
  fallbackMessage: '成交复盘加载失败',
  onActivate: restoreFromRoute
})


// 已经在本页时直接改地址栏——手改 URL、打开一条带不同筛选的分享链接——组件
// 既不会重新挂载也不会重新 activate，只靠 onActivated 跟不上。
//
// 自己写回的 query 与 routeQuery() 一致，所以这里不会把应用筛选变成两次请求；
// 路由名变了说明已经切走，被缓存的实例不该再管地址栏。
const ownRoute = route.name
watch(() => route.query, () => {
  if (route.name !== ownRoute || isQuerySynced(route.query, routeQuery())) return
  restoreFromRoute()
  void reload()
})

async function syncRoute() {
  await syncQuery(routeQuery())
}

async function applyFilters() {
  page.value = 1
  await syncRoute()
  await reload()
}

async function changePage(pagination: TablePaginationConfig) {
  applyPage({ current: pagination.current ?? undefined, pageSize: pagination.pageSize ?? undefined })
  await syncRoute()
  await reload()
}

function openCampaign(group: CampaignSummary) {
  const target = campaignRoute(group)
  if (target) void router.push(target)
}
</script>

<template>
  <main class="operations-page trade-review-page">
    <PageHeader eyebrow="ANALYSIS / EXECUTION TRACE" title="成交复盘" description="以服务端完整 Campaign 为浏览单位，下钻到全部账本 fills 和策略审计事件。" :loading="loading" :refreshed-at="refreshedAt" @refresh="reload" />
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
    <DataState :loading="loading" :error="error" :empty="!campaigns.length" @retry="reload">
      <div class="table-frame"><a-table :columns="campaignColumns" :data-source="campaigns" :row-key="campaignKey" :pagination="tablePagination" :scroll="{ x: 1670 }" @change="changePage" /></div>
    </DataState>

  </main>
</template>

<style scoped lang="scss">
.review-tools { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }.scope-alert { margin-bottom:12px; }.campaign-symbol,.campaign-button { display:block; width:100%; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.campaign-button { padding-inline:0; text-align:left; }.campaign-status { white-space:nowrap; }.net-unavailable,.value-unavailable { color:var(--color-warning); text-decoration:underline dotted; text-underline-offset:3px; }
@media(max-width:700px){.review-tools{align-items:flex-start;flex-direction:column}}
</style>
