<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { CandlestickChart } from 'lucide-vue-next'
import { NButton, NDataTable, NIcon, NPagination, NTag, NTooltip, type DataTableColumns } from 'naive-ui'
import { RouterLink, useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestTradeSummary } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { formatDuration, formatNumber, formatPercent, formatTime, pnlClass } from '@/features/backtests/format'

const route = useRoute()
const researchId = computed(() => String(route.params.researchId))
const symbol = computed(() => String(route.params.symbol))
const page = ref(1)
const pageSize = ref(25)
const query = useQuery({ queryKey: computed(() => ['backtest-trades', researchId.value, symbol.value, page.value, pageSize.value]), queryFn: () => backtestApi.trades(researchId.value, symbol.value, pageSize.value, (page.value - 1) * pageSize.value) })
const columns: DataTableColumns<BacktestTradeSummary> = [
  { title: '开仓时间', key: 'entry_time', width: 180, render: (row) => formatTime(row.entry_time) },
  { title: '均价', key: 'entry_price', width: 120, render: (row) => formatNumber(row.entry_price, 8) },
  { title: '退出时间', key: 'exit_time', width: 180, render: (row) => formatTime(row.exit_time) },
  { title: '退出价', key: 'exit_price', width: 120, render: (row) => formatNumber(row.exit_price, 8) },
  { title: '档位', key: 'filled_tier_count', width: 76, render: (row) => row.filled_tier_count ? `${row.filled_tier_count}档` : '-' },
  { title: '持仓', key: 'holding_seconds', width: 100, render: (row) => formatDuration(row.holding_seconds) },
  { title: '净盈亏', key: 'net_pnl', width: 110, render: (row) => h('span', { class: pnlClass(row.net_pnl) }, formatNumber(row.net_pnl)) },
  { title: '收益率', key: 'net_return', width: 90, render: (row) => formatPercent(row.net_return) },
  { title: '结果', key: 'winner', width: 74, render: (row) => h(NTag, { size: 'small', bordered: false, type: row.winner ? 'success' : 'error' }, { default: () => row.winner ? '盈利' : '亏损' }) },
  { title: '退出原因', key: 'exit_reason', minWidth: 170 },
  { title: '', key: 'action', width: 64, fixed: 'right', render: (row) => h(NTooltip, null, { trigger: () => h(RouterLink, { to: `/backtests/${encodeURIComponent(researchId.value)}/trades/${encodeURIComponent(row.id)}` }, () => h(NButton, { quaternary: true, circle: true, 'aria-label': '打开K线复盘' }, { icon: () => h(NIcon, { component: CandlestickChart }) })), default: () => '打开 K 线复盘' }) }
]
</script>

<template>
    <BacktestPage :title="`${symbol} 交易记录`" :eyebrow="researchId" :back-to="`/backtests/${researchId}/symbols`" :crumbs="[{ label: '回测复盘', to: '/backtests' }, { label: '交易对数据', to: `/backtests/${researchId}/symbols` }, { label: symbol }]">
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" @retry="query.refetch()">
      <div class="table-frame"><NDataTable :columns="columns" :data="query.data.value?.items || []" :row-key="(row: BacktestTradeSummary) => row.id" :scroll-x="1320" striped /><div class="pagination-bar"><span>共 {{ query.data.value?.total || 0 }} 笔</span><NPagination v-model:page="page" v-model:page-size="pageSize" :item-count="query.data.value?.total || 0" show-size-picker :page-sizes="[25, 50, 100]" /></div></div>
    </QueryPanel>
  </BacktestPage>
</template>
