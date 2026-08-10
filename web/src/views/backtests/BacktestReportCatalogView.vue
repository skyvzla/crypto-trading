<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { FileChartColumn, ArrowRight } from 'lucide-vue-next'
import { NButton, NIcon, NTag } from 'naive-ui'
import { useRoute, RouterLink } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'

const route = useRoute()
const researchId = computed(() => String(route.params.researchId))
const query = useQuery({ queryKey: computed(() => ['backtest-reports', researchId.value]), queryFn: () => backtestApi.reports(researchId.value) })
</script>

<template>
  <BacktestPage title="分析报表" :eyebrow="researchId" back-to="/backtests" :crumbs="[{ label: '回测复盘', to: '/backtests' }, { label: '分析报表' }]">
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" @retry="query.refetch()">
      <div class="catalog-list">
        <article v-for="report in query.data.value?.items" :key="report.type" class="catalog-row">
          <NIcon :component="FileChartColumn" size="22" class="catalog-icon" />
          <div class="catalog-copy">
            <div><strong>{{ report.title }}</strong><NTag v-if="report.category" size="small" :bordered="false">{{ report.category }}</NTag></div>
            <p>{{ report.description || report.type }}</p>
          </div>
          <span class="row-count">{{ report.row_count ?? '-' }} 行</span>
          <RouterLink :to="`/backtests/${encodeURIComponent(researchId)}/reports/${encodeURIComponent(report.type)}`">
            <NButton secondary>打开<template #icon><NIcon :component="ArrowRight" /></template></NButton>
          </RouterLink>
        </article>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
