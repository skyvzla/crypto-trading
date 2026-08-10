<script setup lang="ts">
import { computed, h } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { BarChart3, CandlestickChart } from 'lucide-vue-next'
import { Button, Space, Tag, Tooltip, type TableColumnsType } from 'ant-design-vue'
import { RouterLink } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { BacktestResearch } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { formatNumber, formatPercent, formatTime, pnlClass } from '@/features/backtests/format'
import { useBacktestPagination } from '@/features/backtests/pagination'

const { page, pageSize, preservedQuery } = useBacktestPagination(25, 'research')
const query = useQuery({
  queryKey: computed(() => ['backtest-researches', page.value, pageSize.value]),
  queryFn: () => backtestApi.researches(pageSize.value, (page.value - 1) * pageSize.value)
})

const columns: TableColumnsType<BacktestResearch> = [
  { title: '研究记录', key: 'name', dataIndex: 'name', width: 260, customRender: ({ record: row }) => h('div', { class: 'primary-cell' }, [h('strong', row.name), h('small', row.id)]) },
  { title: '策略', key: 'strategy_id', dataIndex: 'strategy_id', width: 150 },
  { title: '状态', key: 'status', dataIndex: 'status', width: 100, customRender: ({ record: row }) => h(Tag, { color: row.status === 'completed' ? 'success' : 'warning' }, () => row.status) },
  { title: '交易对', key: 'symbol_count', dataIndex: 'symbol_count', width: 80 },
  { title: '交易数', key: 'trade_count', dataIndex: 'trade_count', width: 90 },
  { title: '胜率', key: 'win_rate', dataIndex: 'win_rate', width: 90, customRender: ({ record: row }) => formatPercent(row.win_rate) },
  { title: '最佳参数净盈亏', key: 'net_pnl', dataIndex: 'net_pnl', width: 140, customRender: ({ record: row }) => h('span', { class: pnlClass(row.net_pnl) }, formatNumber(row.net_pnl)) },
  { title: '创建时间', key: 'created_at', dataIndex: 'created_at', width: 180, customRender: ({ record: row }) => formatTime(row.created_at) },
  {
    title: '操作', key: 'actions', width: 118, fixed: 'right',
    customRender: ({ record: row }) => h(Space, { size: 4 }, () => [
      h(Tooltip, { title: '查看分析报表' }, () => h(RouterLink, { to: { path: `/backtests/${encodeURIComponent(row.id)}/reports`, query: preservedQuery.value } }, () => h(Button, { type: 'text', shape: 'circle', 'aria-label': '查看分析报表' }, () => h(BarChart3, { size: 16 })))),
      h(Tooltip, { title: '查看交易对' }, () => h(RouterLink, { to: { path: `/backtests/${encodeURIComponent(row.id)}/symbols`, query: preservedQuery.value } }, () => h(Button, { type: 'text', shape: 'circle', 'aria-label': '查看交易对' }, () => h(CandlestickChart, { size: 16 }))))
    ])
  }
]
</script>

<template>
  <BacktestPage title="研究记录" eyebrow="RESEARCH ARCHIVE" :crumbs="[{ label: '回测复盘' }]">
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" @retry="query.refetch()">
      <div class="table-frame">
        <a-table :columns="columns" :data-source="query.data.value?.items || []" row-key="id" :scroll="{ x: 1140 }" :pagination="false" size="middle" />
        <div class="pagination-bar">
          <span>共 {{ query.data.value?.total || 0 }} 条</span>
          <a-pagination v-model:current="page" v-model:page-size="pageSize" :total="query.data.value?.total || 0" show-size-changer :page-size-options="['25', '50', '100']" />
        </div>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
