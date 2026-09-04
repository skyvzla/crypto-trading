<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { CandlestickChart } from 'lucide-vue-next'
import { Button, Tag, Tooltip, type TableColumnsType } from 'ant-design-vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestTradeSummary } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { formatDateTime, formatDuration, formatNumber, formatPercent, pnlClass } from '@/shared/format'
import { useBacktestPagination } from '@/features/backtests/useBacktestPagination'

const route = useRoute()
const router = useRouter()
const researchId = computed(() => (typeof route.params.researchId === 'string' ? route.params.researchId : ''))
const symbol = computed(() => (typeof route.params.symbol === 'string' ? route.params.symbol : ''))
const { page, pageSize, preservedQuery } = useBacktestPagination(25, 'trade')
const backTo = computed(() => ({
  path: `/backtests/${encodeURIComponent(researchId.value)}/symbols`,
  query: preservedQuery.value,
}))
const rootTo = computed(() => ({ path: '/backtests', query: preservedQuery.value }))
const resultFilter = ref(route.query.result === 'win' || route.query.result === 'loss' ? route.query.result : 'all')
const exitReason = ref(typeof route.query.exit_reason === 'string' ? route.query.exit_reason : '')
const minPnl = ref<number | null>(
  typeof route.query.min_pnl === 'string' && Number.isFinite(Number(route.query.min_pnl))
    ? Number(route.query.min_pnl)
    : null,
)
const maxPnl = ref<number | null>(
  typeof route.query.max_pnl === 'string' && Number.isFinite(Number(route.query.max_pnl))
    ? Number(route.query.max_pnl)
    : null,
)
const sortBy = ref(typeof route.query.trade_sort_by === 'string' ? route.query.trade_sort_by : 'entry_time')
const sortOrder = ref<'asc' | 'desc'>(route.query.trade_sort_order === 'asc' ? 'asc' : 'desc')
const tradeFilters = computed(() => ({
  ...(resultFilter.value === 'all' ? {} : { winner: resultFilter.value === 'win' }),
  ...(exitReason.value.trim() ? { exit_reason: exitReason.value.trim() } : {}),
  ...(minPnl.value !== null && Number.isFinite(minPnl.value) ? { min_pnl: minPnl.value } : {}),
  ...(maxPnl.value !== null && Number.isFinite(maxPnl.value) ? { max_pnl: maxPnl.value } : {}),
  sort_by: sortBy.value,
  sort_order: sortOrder.value,
}))
const query = useQuery({
  queryKey: computed(() => [
    'backtest-trades',
    researchId.value,
    symbol.value,
    page.value,
    pageSize.value,
    tradeFilters.value,
  ]),
  queryFn: () =>
    backtestApi.trades(
      researchId.value,
      symbol.value,
      pageSize.value,
      (page.value - 1) * pageSize.value,
      tradeFilters.value,
    ),
  enabled: computed(() => Boolean(researchId.value && symbol.value)),
})
watch([resultFilter, exitReason, minPnl, maxPnl, sortBy, sortOrder], () => {
  page.value = 1
  void router.replace({
    query: {
      ...route.query,
      result: resultFilter.value === 'all' ? undefined : resultFilter.value,
      exit_reason: exitReason.value || undefined,
      min_pnl: minPnl.value === null ? undefined : String(minPnl.value),
      max_pnl: maxPnl.value === null ? undefined : String(maxPnl.value),
      trade_sort_by: sortBy.value,
      trade_sort_order: sortOrder.value,
    },
  })
})
function onTableChange(
  _: unknown,
  __: unknown,
  sorter: { field?: string; order?: 'ascend' | 'descend' } | Array<{ field?: string; order?: 'ascend' | 'descend' }>,
) {
  const item = Array.isArray(sorter) ? sorter[0] : sorter
  sortBy.value = item?.field ? String(item.field) : 'entry_time'
  sortOrder.value = item?.order === 'ascend' ? 'asc' : 'desc'
}
const columns: TableColumnsType<BacktestTradeSummary> = [
  {
    title: '首笔成交确认',
    key: 'entry_time',
    dataIndex: 'entry_time',
    width: 180,
    sorter: true,
    defaultSortOrder: 'descend',
    customRender: ({ record: row }) => formatDateTime(row.entry_time),
  },
  {
    title: '均价',
    key: 'entry_price',
    dataIndex: 'entry_price',
    width: 120,
    sorter: true,
    customRender: ({ record: row }) => formatNumber(row.entry_price, 8),
  },
  {
    title: '退出时间',
    key: 'exit_time',
    dataIndex: 'exit_time',
    width: 180,
    sorter: true,
    customRender: ({ record: row }) => formatDateTime(row.exit_time),
  },
  {
    title: '退出价',
    key: 'exit_price',
    dataIndex: 'exit_price',
    width: 120,
    sorter: true,
    customRender: ({ record: row }) => formatNumber(row.exit_price, 8),
  },
  {
    title: '入场成交笔数',
    key: 'entry_fill_count',
    dataIndex: 'entry_fill_count',
    width: 125,
    sorter: true,
    customRender: ({ record: row }) => row.entry_fill_count ?? '-',
  },
  {
    title: '持仓',
    key: 'holding_seconds',
    dataIndex: 'holding_seconds',
    width: 100,
    sorter: true,
    customRender: ({ record: row }) => formatDuration(row.holding_seconds),
  },
  {
    title: '净盈亏',
    key: 'net_pnl',
    dataIndex: 'net_pnl',
    width: 110,
    sorter: true,
    customRender: ({ record: row }) => h('span', { class: pnlClass(row.net_pnl) }, formatNumber(row.net_pnl)),
  },
  {
    title: '收益率',
    key: 'net_return',
    dataIndex: 'net_return',
    width: 90,
    sorter: true,
    customRender: ({ record: row }) => formatPercent(row.net_return),
  },
  {
    title: '结果',
    key: 'winner',
    dataIndex: 'winner',
    width: 74,
    sorter: true,
    customRender: ({ record: row }) =>
      h(Tag, { color: row.winner ? 'success' : 'error' }, () => (row.winner ? '盈利' : '亏损')),
  },
  { title: '退出原因', key: 'exit_reason', dataIndex: 'exit_reason', minWidth: 170, sorter: true },
  {
    title: '',
    key: 'action',
    width: 64,
    fixed: 'right',
    customRender: ({ record: row }) =>
      h(Tooltip, { title: '打开 K 线复盘' }, () =>
        h(
          RouterLink,
          {
            to: {
              path: `/backtests/${encodeURIComponent(researchId.value)}/trades/${encodeURIComponent(row.id)}`,
              query: preservedQuery.value,
            },
          },
          () =>
            h(Button, { type: 'text', shape: 'circle', 'aria-label': '打开K线复盘' }, () =>
              h(CandlestickChart, { size: 16 }),
            ),
        ),
      ),
  },
]
</script>

<template>
  <BacktestPage
    :title="`${symbol} 交易记录`"
    :eyebrow="researchId"
    :back-to="backTo"
    :crumbs="[{ label: '回测复盘', to: rootTo }, { label: '交易对数据', to: backTo }, { label: symbol }]"
  >
    <QueryPanel
      :pending="query.isPending.value"
      :error="query.error.value"
      :empty="query.data.value?.items.length === 0"
      @retry="query.refetch()"
    >
      <div class="trade-table-tools">
        <a-select
          v-model:value="resultFilter"
          :options="[
            { value: 'all', label: '全部结果' },
            { value: 'win', label: '仅盈利' },
            { value: 'loss', label: '仅亏损' },
          ]"
        />
        <a-input v-model:value="exitReason" allow-clear placeholder="筛选退出原因" />
        <a-input-number v-model:value="minPnl" placeholder="最低盈亏 U" :controls="false" />
        <span>至</span>
        <a-input-number v-model:value="maxPnl" placeholder="最高盈亏 U" :controls="false" />
      </div>
      <div class="table-frame">
        <a-table
          :columns="columns"
          :data-source="query.data.value?.items || []"
          row-key="id"
          :scroll="{ x: 1320 }"
          :pagination="false"
          size="middle"
          @change="onTableChange"
        />
        <div class="pagination-bar">
          <span>共 {{ query.data.value?.total || 0 }} 笔</span
          ><a-pagination
            v-model:current="page"
            v-model:page-size="pageSize"
            :total="query.data.value?.total || 0"
            show-size-changer
            :page-size-options="['25', '50', '100']"
          />
        </div>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
