<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { ArrowRight } from 'lucide-vue-next'
import { NButton, NDataTable, NIcon, NPagination, NTooltip, type DataTableColumns } from 'naive-ui'
import { RouterLink, useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestSymbolSummary } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { formatDuration, formatNumber, formatPercent, pnlClass } from '@/features/backtests/format'

const route = useRoute()
const researchId = computed(() => String(route.params.researchId))
const page = ref(1)
const pageSize = ref(25)
const query = useQuery({ queryKey: computed(() => ['backtest-symbols', researchId.value, page.value, pageSize.value]), queryFn: () => backtestApi.symbols(researchId.value, pageSize.value, (page.value - 1) * pageSize.value) })
const columns: DataTableColumns<BacktestSymbolSummary> = [
  { title: '交易对', key: 'symbol', width: 150, render: (row) => h('strong', { class: 'symbol-name' }, row.symbol) },
  { title: '交易数', key: 'trade_count', width: 90, sorter: 'default' },
  { title: '胜率', key: 'win_rate', width: 90, render: (row) => formatPercent(row.win_rate) },
  { title: '净盈亏', key: 'net_pnl', width: 120, render: (row) => h('span', { class: pnlClass(row.net_pnl) }, formatNumber(row.net_pnl)) },
  { title: '平均盈利', key: 'average_win', width: 110, render: (row) => formatNumber(row.average_win) },
  { title: '平均亏损', key: 'average_loss', width: 110, render: (row) => formatNumber(row.average_loss) },
  { title: '平均持仓', key: 'average_holding_seconds', width: 110, render: (row) => formatDuration(row.average_holding_seconds) },
  { title: '三档成交', key: 'full_tier_fill_rate', width: 100, render: (row) => formatPercent(row.full_tier_fill_rate) },
  { title: '', key: 'action', width: 64, fixed: 'right', render: (row) => h(NTooltip, null, { trigger: () => h(RouterLink, { to: `/backtests/${encodeURIComponent(researchId.value)}/symbols/${encodeURIComponent(row.symbol)}/trades` }, () => h(NButton, { quaternary: true, circle: true, 'aria-label': '查看交易记录' }, { icon: () => h(NIcon, { component: ArrowRight }) })), default: () => '查看交易记录' }) }
]
</script>

<template>
  <BacktestPage title="交易对数据" :eyebrow="researchId" back-to="/backtests" :crumbs="[{ label: '回测复盘', to: '/backtests' }, { label: '交易对数据' }]">
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" empty-text="本次研究没有产生交易" @retry="query.refetch()">
      <div class="table-frame"><NDataTable :columns="columns" :data="query.data.value?.items || []" :row-key="(row: BacktestSymbolSummary) => row.symbol" :scroll-x="1050" striped /><div class="pagination-bar"><span>共 {{ query.data.value?.total || 0 }} 个交易对</span><NPagination v-model:page="page" v-model:page-size="pageSize" :item-count="query.data.value?.total || 0" show-size-picker :page-sizes="[25, 50, 100]" /></div></div>
    </QueryPanel>
  </BacktestPage>
</template>
