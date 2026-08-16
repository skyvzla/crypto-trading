<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { BarChart3, Info, Scale } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { DailyPnL, PerformanceSummary } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import MetricTile from '@/features/operations/MetricTile.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { asNumber, formatMoney, formatPercent, formatRatio, pnlClass } from '@/features/operations/format'

const route = useRoute()
const router = useRouter()
const end = new Date()
const start = new Date(end)
start.setDate(start.getDate() - 29)
const isoDate = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`

const filters = ref<OperationFilters>({
  account_id: String(route.query.account_id ?? ''),
  strategy_id: String(route.query.strategy_id ?? ''),
  symbol: String(route.query.symbol ?? '')
})
const startDate = ref(String(route.query.start_date ?? isoDate(start)))
const endDate = ref(String(route.query.end_date ?? isoDate(end)))
const activeTab = ref(String(route.query.tab ?? 'overview'))
const summary = ref<PerformanceSummary | null>(null)
const daily = ref<DailyPnL[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)

const query = computed(() => ({
  account_id: filters.value.account_id.trim(),
  ...(filters.value.strategy_id.trim() ? { strategy_id: filters.value.strategy_id.trim() } : {}),
  ...(filters.value.symbol.trim() ? { symbol: filters.value.symbol.trim() } : {}),
  start_date: startDate.value,
  end_date: endDate.value
}))
const sampleSize = computed(() => (summary.value?.win_count ?? 0) + (summary.value?.loss_count ?? 0) + (summary.value?.flat_count ?? 0))
const maxDailyAbs = computed(() => Math.max(1, ...daily.value.map((item) => Math.abs(asNumber(item.net_pnl)))))
const scopeText = computed(() => summary.value?.metric_scope || '完整且已结束的 Campaign / 交易轮次')

async function load() {
  if (!filters.value.account_id.trim()) {
    summary.value = null
    daily.value = []
    error.value = null
    return
  }
  if (!startDate.value || !endDate.value || startDate.value > endDate.value) {
    error.value = '请选择有效的开始和结束日期'
    return
  }
  loading.value = true
  error.value = null
  try {
    const [performance, points] = await Promise.all([
      operationsApi.performance(query.value),
      operationsApi.dailyPnl({ ...query.value, timezone: 'Asia/Shanghai' })
    ])
    summary.value = performance
    daily.value = points
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '绩效数据加载失败'
  } finally {
    loading.value = false
  }
}

async function applyFilters() {
  await router.replace({ query: { ...query.value, tab: activeTab.value } })
  await load()
}

async function changeTab(key: string) {
  activeTab.value = key
  await router.replace({ query: { ...query.value, tab: key } })
}

onMounted(load)
</script>

<template>
  <main class="operations-page performance-page">
    <PageHeader eyebrow="ANALYSIS / CAMPAIGN METRICS" title="绩效分析" description="统计单位固定为完整且已结束的 Campaign；胜率和盈亏比用于诊断，不作为策略自动启停门槛。" :loading="loading" :refreshed-at="refreshedAt" @refresh="load" />
    <FilterBar v-model="filters" account-required @apply="applyFilters" />
    <div class="date-filter">
      <label><span>开始日期</span><a-date-picker :value="startDate" value-format="YYYY-MM-DD" @update:value="startDate = String($event ?? '')" /></label>
      <span class="date-rule">ASIA/SHANGHAI</span>
      <label><span>结束日期</span><a-date-picker :value="endDate" value-format="YYYY-MM-DD" @update:value="endDate = String($event ?? '')" /></label>
      <a-button @click="applyFilters">应用日期</a-button>
    </div>
    <a-alert v-if="!filters.account_id.trim()" type="info" show-icon message="请输入账户 ID" description="绩效接口不会跨账户混算；账户是必填筛选条件。" />

    <template v-else>
      <a-tabs :active-key="activeTab" @change="changeTab">
        <a-tab-pane key="overview" tab="绩效概览" />
        <a-tab-pane key="quality" tab="交易质量" />
        <a-tab-pane key="breakdown" tab="交易对与分类" />
      </a-tabs>
      <DataState :loading="loading" :error="error" :empty="!summary" @retry="load">
        <template v-if="summary">
          <div class="metric-scope"><Info :size="14" /><span>样本口径：<strong>{{ scopeText }}</strong></span><span>时间：{{ startDate }} → {{ endDate }}</span><span>样本：{{ sampleSize }} 轮次 / {{ summary.total_fills }} fills</span><span>成本：净 PnL 已扣账本手续费</span></div>
          <a-alert v-if="sampleSize < 30" type="warning" show-icon :message="`样本不足：当前仅 ${sampleSize} 个已结束轮次`" description="指标仍按真实样本展示，但不建议据此自动改变策略准入。" class="sample-warning" />

          <template v-if="activeTab === 'overview'">
            <section class="metric-grid">
              <MetricTile label="净 PnL" :value="formatMoney(summary.net_pnl)" :tone="asNumber(summary.net_pnl) >= 0 ? 'positive' : 'negative'" hint="已实现净额" />
              <MetricTile label="总已实现 PnL" :value="formatMoney(summary.total_realized_pnl)" hint="扣费前已实现" />
              <MetricTile label="交易成本" :value="formatMoney(summary.total_commission)" tone="warning" hint="账本手续费" />
              <MetricTile label="最大回撤" :value="formatMoney(summary.max_drawdown)" tone="negative" hint="Campaign 净值序列" />
              <MetricTile label="纳入轮次" :value="String(summary.candidate_campaigns - summary.excluded_campaigns)" :hint="`候选 ${summary.candidate_campaigns} / 排除 ${summary.excluded_campaigns}`" />
              <MetricTile label="未归属 fills" :value="String(summary.unattributed_fills)" :tone="summary.unattributed_fills ? 'warning' : 'neutral'" hint="不计入 Campaign 指标" />
            </section>
            <section class="data-card daily-chart-card">
              <div class="data-card-heading"><div><h2>每日已实现净 PnL</h2><p>日界线 Asia/Shanghai；缺失日期不插值</p></div><span class="heading-meta">{{ daily.length }} DATA POINTS</span></div>
              <div v-if="daily.length" class="daily-bars">
                <div v-for="point in daily" :key="point.date" class="daily-bar-item" :title="`${point.date}: ${formatMoney(point.net_pnl)}`">
                  <div class="bar-track"><i :class="pnlClass(point.net_pnl)" :style="{ height: `${Math.max(3, Math.abs(asNumber(point.net_pnl)) / maxDailyAbs * 100)}%` }" /></div>
                  <span>{{ point.date.slice(5) }}</span>
                </div>
              </div>
              <div v-else class="chart-empty">所选日期范围没有按日收益数据</div>
            </section>
          </template>

          <template v-else-if="activeTab === 'quality'">
            <section class="quality-hero">
              <article><span>胜率</span><strong>{{ formatPercent(summary.win_rate) }}</strong><p>{{ summary.win_count }} 盈 / {{ summary.loss_count }} 亏 / {{ summary.flat_count }} 平</p></article>
              <div class="quality-cross"><Scale :size="24" /><span>必须与样本和收益尺度一起阅读</span></div>
              <article><span>盈亏比</span><strong>{{ formatRatio(summary.payoff_ratio) }}</strong><p>平均盈利 ÷ 平均亏损绝对值</p></article>
            </section>
            <section class="metric-grid quality-metrics">
              <MetricTile label="平均盈利" :value="formatMoney(summary.avg_win)" tone="positive" :hint="`${summary.win_count} 个盈利轮次`" />
              <MetricTile label="平均亏损" :value="formatMoney(summary.avg_loss)" tone="negative" :hint="`${summary.loss_count} 个亏损轮次`" />
              <MetricTile label="单轮期望值" :value="formatMoney(summary.expectancy)" :tone="asNumber(summary.expectancy) >= 0 ? 'positive' : 'negative'" hint="胜率 × 均盈 + 败率 × 均亏" />
              <MetricTile label="Profit Factor" :value="formatRatio(summary.profit_factor)" hint="总盈利 ÷ 总亏损绝对值" />
            </section>
            <a-alert v-if="summary.loss_count === 0" type="info" show-icon message="没有亏损样本，盈亏比和 Profit Factor 显示为 —，不会显示无穷大。" />
          </template>

          <template v-else>
            <section class="breakdown-panel">
              <BarChart3 :size="30" />
              <div><h2>{{ filters.symbol ? `${filters.symbol} 绩效` : '交易对与分类分解' }}</h2><p v-if="filters.symbol">上方指标已按当前交易对筛选；更换交易对可进行逐项检查。</p><p v-else>当前 `/performance` 只返回所选账户/策略/交易对的单组聚合，尚未返回按交易对、Category、Subcategory、方向或退出原因的分组结果。页面不通过客户端抽样拼出不完整排行榜。</p></div>
            </section>
            <div class="breakdown-actions"><RouterLink to="/categories"><a-button>查看分类目录</a-button></RouterLink><a-button @click="activeTab = 'overview'">查看当前筛选绩效</a-button></div>
          </template>
        </template>
      </DataState>
    </template>
  </main>
</template>

<style scoped lang="scss">
.date-filter { display:flex; align-items:flex-end; gap:9px; margin:-4px 0 14px; }.date-filter label { display:grid; gap:4px; }.date-filter label span { color:var(--muted); font-size:10px; }.date-rule { align-self:center; margin-top:15px; color:#b38732; font:9px "IBM Plex Mono",monospace; }.metric-scope { display:flex; align-items:center; flex-wrap:wrap; gap:8px 18px; margin-bottom:10px; padding:9px 11px; border:1px solid var(--line); background:var(--surface); color:var(--muted); font-size:10px; }.metric-scope strong { color:var(--text); }.sample-warning { margin-bottom:12px; }.daily-chart-card { margin-top:14px; }.daily-bars { display:flex; align-items:stretch; gap:4px; height:230px; padding:16px 14px 9px; overflow-x:auto; }.daily-bar-item { display:flex; flex:1 0 26px; min-width:26px; flex-direction:column; align-items:center; gap:7px; }.bar-track { display:flex; align-items:flex-end; width:100%; height:180px; border-bottom:1px solid var(--line); background:linear-gradient(to top,transparent 49.7%,var(--line) 50%,transparent 50.3%); }.bar-track i { display:block; width:100%; min-height:3px; background:#7b8997; opacity:.84; }.bar-track i.value-positive { background:#2f9d72; }.bar-track i.value-negative { background:#bd625b; }.daily-bar-item span { color:var(--muted); font:8px "IBM Plex Mono",monospace; transform:rotate(-38deg); }.chart-empty { display:grid; place-items:center; height:190px; color:var(--muted); }.quality-hero { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:16px; margin-bottom:14px; }.quality-hero article { min-height:155px; padding:20px; border:1px solid var(--line); border-top:3px solid #b38732; background:var(--surface); text-align:center; }.quality-hero span { color:var(--muted); font-size:11px; }.quality-hero strong { display:block; margin:12px 0 8px; font:600 34px "IBM Plex Mono",monospace; }.quality-hero p { margin:0; color:var(--muted); font-size:10px; }.quality-cross { display:grid; place-items:center; gap:6px; max-width:130px; color:#b38732; text-align:center; }.quality-cross span { font-size:9px; }.quality-metrics { margin-bottom:12px; }.breakdown-panel { display:flex; align-items:center; gap:16px; min-height:180px; padding:24px; border:1px dashed var(--line); background:var(--surface); }.breakdown-panel svg { flex:0 0 auto; color:#b38732; }.breakdown-panel h2 { margin:0 0 7px; font-size:16px; }.breakdown-panel p { margin:0; color:var(--muted); line-height:1.7; }.breakdown-actions { display:flex; gap:8px; margin-top:10px; }
@media(max-width:650px){.date-filter{align-items:stretch;flex-direction:column}.date-rule{align-self:flex-start;margin:0}.quality-hero{grid-template-columns:1fr}.quality-cross{max-width:none}}
</style>
