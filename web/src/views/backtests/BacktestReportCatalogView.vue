<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { FileChartColumn, ArrowRight } from 'lucide-vue-next'
import { useRoute, RouterLink } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'

const route = useRoute()
const researchId = computed(() => typeof route.params.researchId === 'string' ? route.params.researchId : '')
const rootTo = computed(() => ({ path: '/backtests', query: route.query }))
const query = useQuery({ queryKey: computed(() => ['backtest-reports', researchId.value]), queryFn: () => backtestApi.reports(researchId.value), enabled: computed(() => Boolean(researchId.value)) })
</script>

<template>
  <BacktestPage title="分析报表" :eyebrow="researchId" :back-to="rootTo" :crumbs="[{ label: '回测复盘', to: rootTo }, { label: '分析报表' }]">
    <QueryPanel :pending="query.isPending.value" :error="query.error.value" :empty="query.data.value?.items.length === 0" @retry="query.refetch()">
      <div class="catalog-list">
        <article v-for="report in query.data.value?.items" :key="report.type" class="catalog-row">
          <FileChartColumn :size="22" class="catalog-icon" />
          <div class="catalog-copy">
            <div><strong>{{ report.title }}</strong><a-tag v-if="report.category" color="blue">{{ report.category }}</a-tag></div>
            <p>{{ report.description || report.type }}</p>
          </div>
          <span class="row-count">{{ report.row_count ?? '-' }} 行</span>
          <RouterLink :to="{ path: `/backtests/${encodeURIComponent(researchId)}/reports/${encodeURIComponent(report.type)}`, query: route.query }">
            <a-button>打开<template #icon><ArrowRight :size="16" /></template></a-button>
          </RouterLink>
        </article>
      </div>
    </QueryPanel>
  </BacktestPage>
</template>
