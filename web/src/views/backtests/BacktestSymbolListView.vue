<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { ArrowRight } from 'lucide-vue-next'
import { Button, Tooltip, type TableColumnsType } from 'ant-design-vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestSymbolSummary } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { formatDuration, formatNumber, formatPercent, pnlClass } from '@/features/backtests/format'
import { useBacktestPagination } from '@/features/backtests/pagination'

const route = useRoute()
const router = useRouter()
const researchId = computed(() => typeof route.params.researchId === 'string' ? route.params.researchId : '')
const { page, pageSize, preservedQuery } = useBacktestPagination(25, 'symbol')
const rootTo = computed(() => ({ path: '/backtests', query: preservedQuery.value }))
const initialSymbolFilter = typeof route.query.symbol_filter === 'string' ? route.query.symbol_filter : ''
const symbolInput = ref(initialSymbolFilter)
const symbolFilter = ref(initialSymbolFilter)
const sortBy = ref(typeof route.query.sort_by === 'string' ? route.query.sort_by : 'net_pnl')
const sortOrder = ref<'asc' | 'desc'>(route.query.sort_order === 'asc' ? 'asc' : 'desc')
const query = useQuery({
  queryKey: computed(() => ['backtest-symbols', researchId.value, page.value, pageSize.value, symbolFilter.value, sortBy.value, sortOrder.value]),
  queryFn: () => backtestApi.symbols(researchId.value, pageSize.value, (page.value - 1) * pageSize.value, symbolFilter.value, sortBy.value, sortOrder.value),
  enabled: computed(() => Boolean(researchId.value))
})
function onSearch(value: string) {
  symbolFilter.value = value.trim().toUpperCase()
  page.value = 1
}
function onTableChange(_: unknown, __: unknown, sorter: { field?: string; order?: 'ascend' | 'descend' } | Array<{ field?: string; order?: 'ascend' | 'descend' }>) {
  const item = Array.isArray(sorter) ? sorter[0] : sorter
  sortBy.value = item?.field ? String(item.field) : 'net_pnl'
  sortOrder.value = item?.order === 'ascend' ? 'asc' : 'desc'
  page.value = 1
}
watch([symbolFilter, sortBy, sortOrder], ([nextSymbol, nextSort, nextOrder]) => {
  const nextQuery = { ...route.query, symbol_filter: nextSymbol || undefined, sort_by: nextSort, sort_order: nextOrder }
  void router.replace({ query: nextQuery })
})
const columns: TableColumnsType<BacktestSymbolSummary> = [
  { title: '交易对', key: 'symbol', dataIndex: 'symbol', width: 150, sorter: true, customRender: ({ record: row }) => h('strong', { class: 'symbol-name' }, row.symbol) },
  { title: '交易数', key: 'trade_count', dataIndex: 'trade_count', width: 90, sorter: true },
  { title: '胜率', key: 'win_rate', dataIndex: 'win_rate', width: 90, sorter: true, customRender: ({ record: row }) => formatPercent(row.win_rate) },
  { title: '净盈亏', key: 'net_pnl', dataIndex: 'net_pnl', width: 120, sorter: true, defaultSortOrder: 'descend', customRender: ({ record: row }) => h('span', { class: pnlClass(row.net_pnl) }, formatNumber(row.net_pnl)) },
  { title: '平均盈利', key: 'average_win', dataIndex: 'average_win', width: 110, sorter: true, customRender: ({ record: row }) => formatNumber(row.average_win) },
  { title: '平均亏损', key: 'average_loss', dataIndex: 'average_loss', width: 110, sorter: true, customRender: ({ record: row }) => formatNumber(row.average_loss) },
  { title: '平均持仓', key: 'average_holding_seconds', dataIndex: 'average_holding_seconds', width: 110, sorter: true, customRender: ({ record: row }) => formatDuration(row.average_holding_seconds) },
  { title: '三档成交', key: 'full_tier_fill_rate', dataIndex: 'full_tier_fill_rate', width: 100, sorter: true, customRender: ({ record: row }) => formatPercent(row.full_tier_fill_rate) },
  { title: '', key: 'action', width: 64, fixed: 'right', customRender: ({ record: row }) => h(Tooltip, { title: '查看交易记录' }, () => h(RouterLink, { to: { path: `/backtests/${encodeURIComponent(researchId.value)}/symbols/${encodeURIComponent(row.symbol)}/trades`, query: preservedQuery.value } }, () => h(Button, { type: 'text', shape: 'circle', 'aria-label': '查看交易记录' }, () => h(ArrowRight, { size: 16 })))) }
]
</script>

<template>
  <BacktestPage title="交易对数据" :eyebrow="researchId" :back-to="rootTo" :crumbs="[{ label: '回测复盘', to: rootTo }, { label: '交易对数据' }]">
    <div class="symbol-table-tools">
      <a-input-search v-model:value="symbolInput" allow-clear placeholder="筛选交易对，例如 AKE 或 USDT" enter-button="筛选" @search="onSearch" />
      <a-button v-if="symbolFilter" type="link" @click="symbolInput = ''; onSearch('')">清除筛选</a-button>
    </div>
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" empty-text="本次研究没有产生交易" @retry="query.refetch()">
      <div class="table-frame"><a-table :columns="columns" :data-source="query.data.value?.items || []" row-key="symbol" :scroll="{ x: 1050 }" :pagination="false" size="middle" @change="onTableChange" /><div class="pagination-bar"><span>共 {{ query.data.value?.total || 0 }} 个交易对</span><a-pagination v-model:current="page" v-model:page-size="pageSize" :total="query.data.value?.total || 0" show-size-changer :page-size-options="['25', '50', '100']" /></div></div>
    </QueryPanel>
  </BacktestPage>
</template>
