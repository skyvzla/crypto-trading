<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronLeft, ChevronRight, Clock3 } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { DailyPnL } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import MetricTile from '@/features/operations/MetricTile.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import PnlCalendar from '@/features/operations/PnlCalendar.vue'
import { asNumber, formatMoney } from '@/features/operations/format'

const route = useRoute()
const router = useRouter()
const shanghaiParts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).formatToParts(new Date())
const shanghaiMonth = `${shanghaiParts.find((item) => item.type === 'year')?.value}-${shanghaiParts.find((item) => item.type === 'month')?.value}`
const initial = /^\d{4}-\d{2}$/.test(String(route.query.month ?? '')) ? String(route.query.month) : shanghaiMonth
const year = ref(Number(initial.slice(0, 4)))
const month = ref(Number(initial.slice(5, 7)))
const filters = ref<OperationFilters>({ account_id: String(route.query.account_id ?? ''), strategy_id: String(route.query.strategy_id ?? ''), symbol: String(route.query.symbol ?? '') })
const rows = ref<DailyPnL[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)

const startDate = computed(() => `${year.value}-${String(month.value).padStart(2, '0')}-01`)
const endDate = computed(() => `${year.value}-${String(month.value).padStart(2, '0')}-${String(new Date(year.value, month.value, 0).getDate()).padStart(2, '0')}`)
const monthKey = computed(() => `${year.value}-${String(month.value).padStart(2, '0')}`)
const monthNetAvailable = computed(() => rows.value.every((item) => item.net_pnl != null))
const monthNet = computed(() => monthNetAvailable.value ? rows.value.reduce((sum, item) => sum + asNumber(item.net_pnl), 0) : null)
const wins = computed(() => rows.value.filter((item) => asNumber(item.net_pnl) > 0).length)
const losses = computed(() => rows.value.filter((item) => asNumber(item.net_pnl) < 0).length)
const campaignCount = computed(() => rows.value.reduce((sum, item) => sum + item.campaign_count, 0))
const fillCount = computed(() => rows.value.reduce((sum, item) => sum + item.fill_count, 0))
const filtersQuery = computed(() => ({
  ...(filters.value.account_id.trim() ? { account_id: filters.value.account_id.trim() } : {}),
  ...(filters.value.strategy_id.trim() ? { strategy_id: filters.value.strategy_id.trim() } : {}),
  ...(filters.value.symbol.trim() ? { symbol: filters.value.symbol.trim() } : {})
}))

async function load() {
  if (!filters.value.account_id.trim()) { rows.value = []; error.value = null; return }
  loading.value = true
  error.value = null
  try {
    rows.value = await operationsApi.dailyPnl({ account_id: filters.value.account_id.trim(), strategy_id: filtersQuery.value.strategy_id, symbol: filtersQuery.value.symbol, start_date: startDate.value, end_date: endDate.value, timezone: 'Asia/Shanghai' })
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '收益日历加载失败'
  } finally { loading.value = false }
}

async function syncAndLoad() {
  await router.replace({ query: { ...filtersQuery.value, month: monthKey.value } })
  await load()
}

async function moveMonth(delta: number) {
  const next = new Date(year.value, month.value - 1 + delta, 1)
  year.value = next.getFullYear()
  month.value = next.getMonth() + 1
  await syncAndLoad()
}

function openDay(date: string) {
  void router.push({ path: '/trades', query: { ...filtersQuery.value, date } })
}

onMounted(load)
</script>

<template>
  <main class="operations-page calendar-page">
    <PageHeader eyebrow="OPERATIONS / DAILY PNL" title="收益日历" description="按 Asia/Shanghai 自然日查看已实现净收益；收益为空与接口异常明确区分。" :loading="loading" :refreshed-at="refreshedAt" @refresh="load">
      <template #actions><a-button @click="router.push({ path: '/overview', query: filtersQuery })">返回运行总览</a-button></template>
    </PageHeader>
    <FilterBar v-model="filters" account-required @apply="syncAndLoad" />
    <a-alert v-if="!filters.account_id.trim()" type="info" show-icon message="请输入账户 ID 后读取收益日历" description="不同账户的收益不会在前端混算。" />
    <template v-else>
      <div class="calendar-toolbar"><a-button aria-label="上个月" @click="moveMonth(-1)"><template #icon><ChevronLeft :size="15" /></template></a-button><strong>{{ year }} 年 {{ month }} 月</strong><a-button aria-label="下个月" @click="moveMonth(1)"><template #icon><ChevronRight :size="15" /></template></a-button><span><Clock3 :size="13" /> 日界线 Asia/Shanghai</span></div>
      <section class="metric-grid calendar-metrics">
        <MetricTile label="本月累计净 PnL" :value="formatMoney(monthNet)" :tone="monthNet == null ? 'warning' : monthNet >= 0 ? 'positive' : 'negative'" :hint="monthNet == null ? '存在无法统一计价的日期' : '按闭合 Campaign 日净额求和'" />
        <MetricTile label="盈利日" :value="String(wins)" tone="positive" hint="净 PnL > 0" />
        <MetricTile label="亏损日" :value="String(losses)" tone="negative" hint="净 PnL < 0" />
        <MetricTile label="闭合 Campaign" :value="String(campaignCount)" :hint="`${fillCount} 笔关联 fills`" />
      </section>
      <div class="status-strip"><Clock3 :size="13" /><span>完整且已闭合的 Campaign 按 <strong>closed_at 上海自然日</strong>归属 {{ startDate }} → {{ endDate }}；净额仅扣同币种 USDT 手续费，资金费尚无权威事实。</span></div>
      <DataState :loading="loading" :error="error" @retry="load">
        <section class="data-card full-calendar"><PnlCalendar :year="year" :month="month" :rows="rows" @day="openDay" /><div v-if="!rows.length" class="calendar-empty">本月没有已实现收益记录；点击任意日期仍可进入成交复盘。</div></section>
      </DataState>
    </template>
  </main>
</template>

<style scoped lang="scss">
.calendar-toolbar { display:flex; align-items:center; gap:8px; margin:2px 0 12px; }.calendar-toolbar strong { min-width:120px; text-align:center; font:14px "IBM Plex Mono",monospace; }.calendar-toolbar > span { display:flex; align-items:center; gap:5px; margin-left:auto; color:#b38732; font:10px "IBM Plex Mono",monospace; }.calendar-metrics { margin-bottom:10px; }.status-strip { margin-bottom:10px; }.full-calendar { margin-top:2px; }.calendar-empty { padding:0 14px 14px; color:var(--muted); text-align:center; font-size:11px; }
@media(max-width:540px){.calendar-toolbar>span{width:100%;margin:4px 0 0}.calendar-toolbar{flex-wrap:wrap}}
</style>
