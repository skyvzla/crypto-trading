<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Tag, type TableColumnsType, type TablePaginationConfig } from 'ant-design-vue'
import { operationsApi } from '@/api/operations'
import type { LedgerOrder, LedgerPosition } from '@/api/types'
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
import { asNumber, formatDateTime, formatMoney, pnlClass, sideLabel } from '@/shared/format'

type Detail = { kind: 'position'; item: LedgerPosition } | { kind: 'order'; item: LedgerOrder }
type Tab = 'positions' | 'active' | 'history'

const route = useRoute()
const router = useRouter()
const syncQuery = useQuerySync()
const { filters, query, restore: restoreFilters } = useOperationFilters()
const { page, pageSize, offset, restore: restorePage, apply: applyPage } = usePageParams({ defaultSize: 25, maxSize: 100 })
const activeTab = ref<Tab>(readTab())
const historyStatus = ref(String(route.query.status ?? 'FILLED'))
const positions = ref<LedgerPosition[]>([])
const orders = ref<LedgerOrder[]>([])
const total = ref(0)
const detail = ref<Detail | null>(null)
const detailOpen = ref(false)

function readTab(): Tab {
  const requested = String(route.query.tab ?? 'positions')
  return requested === 'active' || requested === 'history' ? requested : 'positions'
}

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '已成交 FILLED', value: 'FILLED' },
  { label: '已撤销 CANCELED', value: 'CANCELED' },
  { label: '已拒绝 REJECTED', value: 'REJECTED' },
  { label: '已过期 EXPIRED', value: 'EXPIRED' }
]

const tablePagination = computed<TablePaginationConfig>(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showSizeChanger: true,
  pageSizeOptions: ['10', '25', '50', '100'],
  showTotal: (value) => `共 ${value} 条`
}))

const positionColumns: TableColumnsType<LedgerPosition> = [
  { title: '交易对', dataIndex: 'symbol', key: 'symbol', fixed: 'left', width: 125, customRender: ({ record }) => h(Button, { type: 'link', class: 'table-link', onClick: () => openDetail({ kind: 'position', item: record }) }, () => record.symbol) },
  { title: '方向', dataIndex: 'position_side', key: 'position_side', width: 110, customRender: ({ text }) => h(Tag, { color: String(text).toUpperCase() === 'LONG' ? 'green' : 'volcano' }, () => sideLabel(String(text))) },
  { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 130 },
  { title: '入场均价', dataIndex: 'entry_price', key: 'entry_price', width: 135, customRender: ({ text }) => formatMoney(String(text), 6) },
  { title: '标记价格', dataIndex: 'mark_price', key: 'mark_price', width: 135, customRender: ({ text }) => formatMoney(text == null ? null : String(text), 6) },
  { title: '浮动 PnL', dataIndex: 'unrealized_pnl', key: 'unrealized_pnl', width: 130, customCell: (record) => ({ class: pnlClass(record.unrealized_pnl) }), customRender: ({ text }) => formatMoney(text == null ? null : String(text)) },
  { title: '策略', dataIndex: 'strategy_id', key: 'strategy_id', width: 150 },
  { title: '账户', dataIndex: 'account_id', key: 'account_id', width: 150 },
  { title: '最后更新', dataIndex: 'updated_at', key: 'updated_at', width: 185, customRender: ({ text }) => formatDateTime(String(text)) }
]

const orderColumns: TableColumnsType<LedgerOrder> = [
  { title: '订单 / 交易对', key: 'identity', fixed: 'left', width: 190, customRender: ({ record }) => h(Button, { type: 'link', class: 'table-link order-link', onClick: () => openDetail({ kind: 'order', item: record }) }, () => `${record.symbol} · ${record.order_id}`) },
  { title: '状态', dataIndex: 'status', key: 'status', width: 150, customRender: ({ text }) => h(Tag, { color: ['NEW', 'PARTIALLY_FILLED'].includes(String(text)) ? 'gold' : text === 'FILLED' ? 'green' : undefined }, () => String(text)) },
  { title: '方向', dataIndex: 'side', key: 'side', width: 105, customRender: ({ text }) => sideLabel(String(text)) },
  { title: '委托价', dataIndex: 'price', key: 'price', width: 125, customRender: ({ text }) => formatMoney(text == null ? null : String(text), 6) },
  { title: '原始数量', dataIndex: 'quantity', key: 'quantity', width: 125 },
  { title: '已成交', dataIndex: 'filled_quantity', key: 'filled_quantity', width: 125 },
  { title: '剩余', key: 'remaining', width: 125, customRender: ({ record }) => formatMoney(Math.max(0, asNumber(record.quantity) - asNumber(record.filled_quantity)), 6) },
  { title: '策略', dataIndex: 'strategy_id', key: 'strategy_id', width: 145 },
  { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 185, customRender: ({ text }) => formatDateTime(String(text)) }
]

function openDetail(value: Detail) {
  detail.value = value
  detailOpen.value = true
}

function openCampaign(order: LedgerOrder) {
  const target = campaignRoute(order)
  if (target) void router.push(target)
}

/** 三个标签页查的是不同资源，只有分页与筛选口径是共用的。 */
async function fetchActiveTab() {
  const paging = { limit: pageSize.value, offset: offset.value }
  if (activeTab.value === 'positions') {
    return { kind: 'positions' as const, page: await operationsApi.positions({ ...query.value, ...paging }) }
  }
  if (activeTab.value === 'active') {
    return { kind: 'orders' as const, page: await operationsApi.orders({ ...query.value, active_only: true, ...paging }) }
  }
  return {
    kind: 'orders' as const,
    page: await operationsApi.orders({ ...query.value, status: historyStatus.value || undefined, ...paging })
  }
}

/** 本页放进地址栏的内容。写回与「URL 是否被外部改动」共用这一处声明。 */
function routeQuery() {
  return {
    ...query.value,
    tab: activeTab.value,
    page: page.value,
    page_size: pageSize.value,
    ...(activeTab.value === 'history' && historyStatus.value ? { status: historyStatus.value } : {})
  }
}

/** 把地址栏状态同步回本地 ref。 */
function restoreFromRoute() {
  restoreFilters()
  restorePage()
  activeTab.value = readTab()
  historyStatus.value = String(route.query.status ?? 'FILLED')
}

const { loading, error, refreshedAt, reload } = useLedgerLoader(async ({ isStale }) => {
  const result = await fetchActiveTab()
  if (isStale()) return
  if (result.kind === 'positions') positions.value = result.page.items
  else orders.value = result.page.items
  total.value = result.page.total
}, {
  fallbackMessage: '持仓与订单加载失败',
  onActivate: restoreFromRoute
})

// 已经在本页时直接改地址栏——手改 URL、打开一条带不同筛选的分享链接——组件
// 既不会重新挂载也不会重新 activate，只靠 onActivated 跟不上。
//
// 自己写回的 query 与 routeQuery() 一致，所以这里不会把 applyFilters 变成两次
// 请求；路由名变了说明已经切走，被缓存的实例不该再管地址栏。
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
  await syncRoute()
  await reload()
}

async function changeTab(key: string) {
  activeTab.value = key === 'active' || key === 'history' ? key : 'positions'
  page.value = 1
  await applyFilters()
}

async function onTableChange(pagination: TablePaginationConfig) {
  applyPage({ current: pagination.current ?? undefined, pageSize: pagination.pageSize ?? undefined })
  await applyFilters()
}
</script>

<template>
  <main class="operations-page positions-orders-page">
    <PageHeader eyebrow="OPERATIONS / EXPOSURE" title="持仓与订单" description="查看当前风险敞口和交易所订单事实；首期仅查询，不在 Web 发起开仓或平仓。" :loading="loading" :refreshed-at="refreshedAt" @refresh="reload" />
    <FilterBar
      v-model="filters"
      :show-status="activeTab === 'history'"
      :status="historyStatus"
      :status-options="statusOptions"
      @update:status="historyStatus = $event"
      @apply="applyFilters"
    />

    <a-tabs :active-key="activeTab" @change="changeTab">
      <a-tab-pane key="positions" tab="当前持仓" />
      <a-tab-pane key="active" tab="活动订单" />
      <a-tab-pane key="history" tab="历史订单" />
    </a-tabs>
    <div class="result-ledger"><span>{{ total }} 条记录</span><span v-if="activeTab === 'active'">口径：NEW + PARTIALLY_FILLED</span><span v-else-if="activeTab === 'history'">口径：后端订单状态筛选</span></div>
    <DataState :loading="loading" :error="error" :empty="activeTab === 'positions' ? !positions.length : !orders.length" @retry="reload">
      <div class="table-frame">
        <a-table v-if="activeTab === 'positions'" :columns="positionColumns" :data-source="positions" row-key="id" :pagination="tablePagination" :scroll="{ x: 1180 }" size="middle" @change="onTableChange" />
        <a-table v-else :columns="orderColumns" :data-source="orders" row-key="id" :pagination="tablePagination" :scroll="{ x: 1250 }" size="middle" @change="onTableChange" />
      </div>
    </DataState>

    <a-drawer v-model:open="detailOpen" width="min(640px, 94vw)" :title="detail?.kind === 'position' ? '持仓详情' : '订单详情'">
      <template v-if="detail?.kind === 'position'">
        <a-descriptions :column="1" bordered size="small">
          <a-descriptions-item label="交易对">{{ detail.item.symbol }}</a-descriptions-item>
          <a-descriptions-item label="账户 / 策略">{{ detail.item.account_id }} / {{ detail.item.strategy_id }}</a-descriptions-item>
          <a-descriptions-item label="方向 / 数量">{{ sideLabel(detail.item.position_side) }} / {{ detail.item.quantity }}</a-descriptions-item>
          <a-descriptions-item label="入场 / 标记价格">{{ formatMoney(detail.item.entry_price, 6) }} / {{ formatMoney(detail.item.mark_price, 6) }}</a-descriptions-item>
          <a-descriptions-item label="浮动 PnL"><strong :class="pnlClass(detail.item.unrealized_pnl)">{{ formatMoney(detail.item.unrealized_pnl) }}</strong></a-descriptions-item>
          <a-descriptions-item label="杠杆 / 保证金">{{ detail.item.leverage ?? '—' }} / {{ detail.item.margin_type ?? '—' }}</a-descriptions-item>
          <a-descriptions-item label="强平价格">{{ formatMoney(detail.item.liquidation_price, 6) }}</a-descriptions-item>
          <a-descriptions-item label="最后更新时间">{{ formatDateTime(detail.item.updated_at) }}</a-descriptions-item>
        </a-descriptions>
        <a-alert class="drawer-note" type="info" show-icon message="当前持仓接口没有 Campaign 与开仓时间字段，因此不推测持仓时长和所属交易轮次。" />
      </template>
      <template v-else-if="detail?.kind === 'order'">
        <a-descriptions :column="1" bordered size="small">
          <a-descriptions-item label="交易对 / 订单">{{ detail.item.symbol }} / {{ detail.item.order_id }}</a-descriptions-item>
          <a-descriptions-item label="客户端订单 ID"><span class="mono">{{ detail.item.client_order_id }}</span></a-descriptions-item>
          <a-descriptions-item label="账户 / 策略">{{ detail.item.account_id }} / {{ detail.item.strategy_id }}</a-descriptions-item>
          <a-descriptions-item label="Campaign">{{ detail.item.campaign_id || '—' }}</a-descriptions-item>
          <a-descriptions-item label="状态 / 类型">{{ detail.item.status }} / {{ detail.item.order_type }}</a-descriptions-item>
          <a-descriptions-item label="方向 / 数量">{{ sideLabel(detail.item.side) }} / {{ detail.item.quantity }}</a-descriptions-item>
          <a-descriptions-item label="委托 / 成交均价">{{ formatMoney(detail.item.price, 6) }} / {{ formatMoney(detail.item.avg_fill_price, 6) }}</a-descriptions-item>
          <a-descriptions-item label="已成交 / 剩余">{{ detail.item.filled_quantity }} / {{ formatMoney(Math.max(0, asNumber(detail.item.quantity) - asNumber(detail.item.filled_quantity)), 6) }}</a-descriptions-item>
          <a-descriptions-item label="创建 / 更新时间">{{ formatDateTime(detail.item.created_at) }} / {{ formatDateTime(detail.item.updated_at) }}</a-descriptions-item>
        </a-descriptions>
        <a-button v-if="detail.item.campaign_id" class="drawer-link" type="primary" @click="openCampaign(detail.item)">查看该 Campaign 成交</a-button>
      </template>
    </a-drawer>
  </main>
</template>

<style scoped lang="scss">
.result-ledger { display:flex; justify-content:space-between; gap:12px; margin:-6px 0 9px; color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }.drawer-note,.drawer-link { margin-top:16px; }.order-link { max-width:180px; overflow:hidden; text-overflow:ellipsis; }
</style>
