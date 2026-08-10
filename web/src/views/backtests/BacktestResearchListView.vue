<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { BarChart3, CandlestickChart } from 'lucide-vue-next'
import { NButton, NDataTable, NIcon, NPagination, NSpace, NTag, NTooltip, type DataTableColumns } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestResearch } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { formatNumber, formatPercent, formatTime, pnlClass } from '@/features/backtests/format'

const page = ref(1)
const pageSize = ref(25)
const query = useQuery({
  queryKey: computed(() => ['backtest-researches', page.value, pageSize.value]),
  queryFn: () => backtestApi.researches(pageSize.value, (page.value - 1) * pageSize.value)
})

const columns: DataTableColumns<BacktestResearch> = [
  { title: '研究记录', key: 'name', minWidth: 220, render: (row) => h('div', { class: 'primary-cell' }, [h('strong', row.name), h('small', row.id)]) },
  { title: '策略', key: 'strategy_id', width: 150 },
  { title: '状态', key: 'status', width: 100, render: (row) => h(NTag, { size: 'small', bordered: false, type: row.status === 'completed' ? 'success' : 'warning' }, { default: () => row.status }) },
  { title: '交易对', key: 'symbol_count', width: 80 },
  { title: '交易数', key: 'trade_count', width: 90 },
  { title: '胜率', key: 'win_rate', width: 90, render: (row) => formatPercent(row.win_rate) },
  { title: '最佳参数净盈亏', key: 'net_pnl', width: 140, render: (row) => h('span', { class: pnlClass(row.net_pnl) }, formatNumber(row.net_pnl)) },
  { title: '创建时间', key: 'created_at', width: 180, render: (row) => formatTime(row.created_at) },
  {
    title: '操作', key: 'actions', width: 118, fixed: 'right',
    render: (row) => h(NSpace, { size: 4 }, () => [
      h(NTooltip, null, { trigger: () => h(RouterLink, { to: `/backtests/${encodeURIComponent(row.id)}/reports` }, () => h(NButton, { quaternary: true, circle: true, 'aria-label': '查看分析报表' }, { icon: () => h(NIcon, { component: BarChart3 }) })), default: () => '查看分析报表' }),
      h(NTooltip, null, { trigger: () => h(RouterLink, { to: `/backtests/${encodeURIComponent(row.id)}/symbols` }, () => h(NButton, { quaternary: true, circle: true, 'aria-label': '查看交易对' }, { icon: () => h(NIcon, { component: CandlestickChart }) })), default: () => '查看交易对' })
    ])
  }
]
</script>

<template>
  <BacktestPage title="研究记录" eyebrow="RESEARCH ARCHIVE" :crumbs="[{ label: '回测复盘' }]">
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" @retry="query.refetch()">
      <div class="table-frame">
        <NDataTable :columns="columns" :data="query.data.value?.items || []" :row-key="(row: BacktestResearch) => row.id" :scroll-x="1140" striped />
        <div class="pagination-bar">
          <span>共 {{ query.data.value?.total || 0 }} 条</span>
          <NPagination v-model:page="page" v-model:page-size="pageSize" :item-count="query.data.value?.total || 0" show-size-picker :page-sizes="[25, 50, 100]" />
        </div>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
