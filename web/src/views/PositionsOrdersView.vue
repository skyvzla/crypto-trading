<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Tag, type TableColumnsType, type TablePaginationConfig } from 'ant-design-vue'
import { operationsApi } from '@/api/operations'
import type { LedgerOrder, LedgerPosition } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { asNumber, formatDateTime, formatMoney, pnlClass, sideLabel } from '@/features/operations/format'

type Detail = { kind: 'position'; item: LedgerPosition } | { kind: 'order'; item: LedgerOrder }

const route = useRoute()
const router = useRouter()
const filters = ref<OperationFilters>({
  account_id: String(route.query.account_id ?? ''),
  strategy_id: String(route.query.strategy_id ?? ''),
  symbol: String(route.query.symbol ?? '')
})
const activeTab = ref(String(route.query.tab ?? 'positions'))
const historyStatus = ref(String(route.query.status ?? 'FILLED'))
const positions = ref<LedgerPosition[]>([])
const orders = ref<LedgerOrder[]>([])
const total = ref(0)
const currentPage = ref(Math.max(1, Number(route.query.page ?? 1) || 1))
const pageSize = ref(Math.max(10, Math.min(100, Number(route.query.page_size ?? 25) || 25)))
const loading = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)
const detail = ref<Detail | null>(null)
const detailOpen = ref(false)

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '已成交 FILLED', value: 'FILLED' },
  { label: '已撤销 CANCELED', value: 'CANCELED' },
  { label: '已拒绝 REJECTED', value: 'REJECTED' },
  { label: '已过期 EXPIRED', value: 'EXPIRED' }
]

const query = computed(() => ({
  ...(filters.value.account_id.trim() ? { account_id: filters.value.account_id.trim() } : {}),
  ...(filters.value.strategy_id.trim() ? { strategy_id: filters.value.strategy_id.trim() } : {}),
  ...(filters.value.symbol.trim() ? { symbol: filters.value.symbol.trim() } : {})
}))
const tablePagination = computed<TablePaginationConfig>(() => ({
  current: currentPage.value,
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

async function load() {
  loading.value = true
  error.value = null
  try {
    if (activeTab.value === 'positions') {
      const page = await operationsApi.positions({ ...query.value, limit: pageSize.value, offset: (currentPage.value - 1) * pageSize.value })
      positions.value = page.items
      total.value = page.total
    } else if (activeTab.value === 'active') {
      const page = await operationsApi.orders({ ...query.value, active_only: true, limit: pageSize.value, offset: (currentPage.value - 1) * pageSize.value })
      orders.value = page.items
      total.value = page.total
    } else {
      const page = await operationsApi.orders({ ...query.value, status: historyStatus.value || undefined, limit: pageSize.value, offset: (currentPage.value - 1) * pageSize.value })
      orders.value = page.items
      total.value = page.total
    }
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '持仓与订单加载失败'
  } finally {
    loading.value = false
  }
}

async function applyFilters() {
  await router.replace({ query: { ...query.value, tab: activeTab.value, page: currentPage.value, page_size: pageSize.value, ...(activeTab.value === 'history' && historyStatus.value ? { status: historyStatus.value } : {}) } })
  await load()
}

async function changeTab(key: string) {
  activeTab.value = key
  currentPage.value = 1
  await applyFilters()
}

async function onTableChange(pagination: TablePaginationConfig) {
  const nextSize = pagination.pageSize ?? pageSize.value
  if (nextSize !== pageSize.value) currentPage.value = 1
  else currentPage.value = pagination.current ?? 1
  pageSize.value = nextSize
  await applyFilters()
}

onMounted(load)
</script>

<template>
  <main class="operations-page positions-orders-page">
    <PageHeader eyebrow="OPERATIONS / EXPOSURE" title="持仓与订单" description="查看当前风险敞口和交易所订单事实；首期仅查询，不在 Web 发起开仓或平仓。" :loading="loading" :refreshed-at="refreshedAt" @refresh="load" />
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
    <DataState :loading="loading" :error="error" :empty="activeTab === 'positions' ? !positions.length : !orders.length" @retry="load">
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
        <a-button v-if="detail.item.campaign_id" class="drawer-link" type="primary" @click="router.push({ path: '/trades', query: { ...query, campaign_id: detail.item.campaign_id || undefined } })">查看该 Campaign 成交</a-button>
      </template>
    </a-drawer>
  </main>
</template>

<style scoped lang="scss">
.result-ledger { display:flex; justify-content:space-between; gap:12px; margin:-6px 0 9px; color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }.drawer-note,.drawer-link { margin-top:16px; }.order-link { max-width:180px; overflow:hidden; text-overflow:ellipsis; }
</style>
