<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { NDataTable, NPagination, type DataTableColumns } from 'naive-ui'
import { useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { JsonObject, JsonValue, ReportColumn } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { displayValue } from '@/features/backtests/format'

const route = useRoute()
const researchId = computed(() => String(route.params.researchId))
const reportType = computed(() => String(route.params.reportType))
const page = ref(1)
const pageSize = ref(50)
watch(reportType, () => { page.value = 1 })
const query = useQuery({
  queryKey: computed(() => ['backtest-report', researchId.value, reportType.value, page.value, pageSize.value]),
  queryFn: () => backtestApi.report(researchId.value, reportType.value, pageSize.value, (page.value - 1) * pageSize.value)
})
const columns = computed<DataTableColumns<JsonObject>>(() => (query.data.value?.columns || []).map((item) => {
  const column: ReportColumn = typeof item === 'string' ? { key: item } : item
  return { title: column.title || column.label || column.key, key: column.key, minWidth: 120, sorter: column.sortable ? 'default' : undefined, render: (row: JsonObject) => h('span', { class: 'report-value' }, displayValue(row[column.key] as JsonValue, column.type)) }
}))
</script>

<template>
  <BacktestPage :title="query.data.value?.descriptor.title || reportType" :eyebrow="reportType" :back-to="`/backtests/${researchId}/reports`" :crumbs="[{ label: '回测复盘', to: '/backtests' }, { label: '分析报表', to: `/backtests/${researchId}/reports` }, { label: query.data.value?.descriptor.title || reportType }]">
    <p v-if="query.data.value?.descriptor.description" class="page-description">{{ query.data.value.descriptor.description }}</p>
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.rows.length === 0" @retry="query.refetch()">
      <div class="table-frame">
        <NDataTable :columns="columns" :data="query.data.value?.rows || []" :scroll-x="Math.max(900, columns.length * 130)" striped />
        <div class="pagination-bar"><span>共 {{ query.data.value?.total || 0 }} 行</span><NPagination v-model:page="page" v-model:page-size="pageSize" :item-count="query.data.value?.total || 0" show-size-picker :page-sizes="[25, 50, 100]" /></div>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
