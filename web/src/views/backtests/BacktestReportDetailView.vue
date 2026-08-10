<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import type { TableColumnsType } from 'ant-design-vue'
import { useRoute, useRouter } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import type { JsonObject, JsonValue, ReportColumn } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { displayValue } from '@/features/backtests/format'
import { useBacktestPagination } from '@/features/backtests/pagination'
import { hasReportLabel, reportLabel } from '@/features/backtests/reportLabels'

const route = useRoute()
const router = useRouter()
const researchId = computed(() => typeof route.params.researchId === 'string' ? route.params.researchId : '')
const reportType = computed(() => typeof route.params.reportType === 'string' ? route.params.reportType : '')
const { page, pageSize } = useBacktestPagination(50, 'report')
const backTo = computed(() => ({ path: `/backtests/${encodeURIComponent(researchId.value)}/reports`, query: route.query }))
const rootTo = computed(() => ({ path: '/backtests', query: route.query }))
const sortBy = ref(typeof route.query.report_sort_by === 'string' ? route.query.report_sort_by : '')
const sortOrder = ref(route.query.report_sort_order === 'asc' ? 'ascend' : 'descend')
watch(reportType, () => { page.value = 1 })
watch([sortBy, sortOrder], ([nextSort, nextOrder]) => {
  void router.replace({ query: { ...route.query, report_sort_by: nextSort || undefined, report_sort_order: nextOrder === 'ascend' ? 'asc' : 'desc' } })
})
const query = useQuery({
  queryKey: computed(() => ['backtest-report', researchId.value, reportType.value, page.value, pageSize.value, sortBy.value, sortOrder.value]),
  queryFn: () => backtestApi.report(researchId.value, reportType.value, pageSize.value, (page.value - 1) * pageSize.value, sortBy.value, sortOrder.value === 'ascend' ? 'asc' : 'desc'),
  enabled: computed(() => Boolean(researchId.value && reportType.value))
})
const columns = computed<TableColumnsType<JsonObject>>(() => (query.data.value?.columns || []).map((item) => {
  const column: ReportColumn = typeof item === 'string' ? { key: item } : item
  const providedTitle = column.title || column.label || ''
  const title = /[\u4e00-\u9fff]/.test(providedTitle)
    ? providedTitle
    : (hasReportLabel(column.key) ? reportLabel(column.key) : (providedTitle || reportLabel(column.key)))
  return { title, key: column.key, dataIndex: column.key, width: 140, sorter: column.sortable === false ? undefined : true, customRender: ({ record: row }) => h('span', { class: 'report-value' }, displayValue(row[column.key] as JsonValue, column.type)) }
}))
function onTableChange(_: unknown, __: unknown, sorter: { field?: string; order?: string } | Array<{ field?: string; order?: string }>) {
  const item = Array.isArray(sorter) ? sorter[0] : sorter
  sortBy.value = item?.field || ''
  sortOrder.value = item?.order || 'descend'
  page.value = 1
}
</script>

<template>
  <BacktestPage :title="query.data.value?.descriptor.title || reportType" :eyebrow="reportType" :back-to="backTo" :crumbs="[{ label: '回测复盘', to: rootTo }, { label: '分析报表', to: backTo }, { label: query.data.value?.descriptor.title || reportType }]">
    <p v-if="query.data.value?.descriptor.description" class="page-description">{{ query.data.value.descriptor.description }}</p>
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.rows.length === 0" @retry="query.refetch()">
      <div class="table-frame">
        <a-table :columns="columns" :data-source="query.data.value?.rows || []" row-key="trade_id" :scroll="{ x: Math.max(900, columns.length * 130) }" :pagination="false" size="middle" @change="onTableChange" />
        <div class="pagination-bar"><span>共 {{ query.data.value?.total || 0 }} 行</span><a-pagination v-model:current="page" v-model:page-size="pageSize" :total="query.data.value?.total || 0" show-size-changer :page-size-options="['25', '50', '100']" /></div>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
