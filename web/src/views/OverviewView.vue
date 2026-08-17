<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { Activity, CalendarDays, CircleAlert, Database, RadioTower, ShieldCheck } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { DailyPnL, Health, LedgerTrade, PnLSummary, StrategyRuntimeStatus } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import MetricTile from '@/features/operations/MetricTile.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import PnlCalendar from '@/features/operations/PnlCalendar.vue'
import { asNumber, formatDateTime, formatMoney, pnlClass } from '@/features/operations/format'

const route = useRoute()
const router = useRouter()
const nowParts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date()).map((item) => [item.type, item.value]))
const year = Number(nowParts.year)
const month = Number(nowParts.month)
const startDate = `${year}-${String(month).padStart(2, '0')}-01`
const endDate = `${year}-${String(month).padStart(2, '0')}-${String(new Date(year, month, 0).getDate()).padStart(2, '0')}`
const today = `${nowParts.year}-${nowParts.month}-${nowParts.day}`

const filters = ref<OperationFilters>({
  account_id: String(route.query.account_id ?? ''),
  strategy_id: String(route.query.strategy_id ?? ''),
  symbol: String(route.query.symbol ?? '')
})
const loading = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)
const health = ref<Health | null>(null)
const runtimes = ref<StrategyRuntimeStatus[]>([])
const positionsTotal = ref<number | null>(null)
const activeOrdersTotal = ref<number | null>(null)
const recentTrades = ref<LedgerTrade[]>([])
const pnl = ref<PnLSummary | null>(null)
const daily = ref<DailyPnL[]>([])
const unavailableSources = ref<string[]>([])

const query = computed(() => ({
  ...(filters.value.account_id.trim() ? { account_id: filters.value.account_id.trim() } : {}),
  ...(filters.value.strategy_id.trim() ? { strategy_id: filters.value.strategy_id.trim() } : {}),
  ...(filters.value.symbol.trim() ? { symbol: filters.value.symbol.trim() } : {})
}))
const todayPnl = computed(() => daily.value.find((item) => item.date === today))
const closedPnlScope = computed(() => {
  const accountId = filters.value.account_id.trim()
  return accountId ? `账户 ${accountId}` : '全部账户'
})
const monthNetAvailable = computed(() => daily.value.every((item) => item.net_pnl != null))
const monthNet = computed(() => monthNetAvailable.value ? daily.value.reduce((sum, item) => sum + asNumber(item.net_pnl), 0) : null)
const winningDays = computed(() => daily.value.filter((item) => asNumber(item.net_pnl) > 0).length)
const losingDays = computed(() => daily.value.filter((item) => asNumber(item.net_pnl) < 0).length)
const runtimeModes = computed(() => [...new Set(runtimes.value.map((item) => item.mode))])
const unhealthyRuntimes = computed(() => runtimes.value.filter((item) => item.effective_status !== 'running'))
const blockedRuntimes = computed(() => runtimes.value.filter((item) => !item.entry_enabled || item.halted))

async function load() {
  loading.value = true
  error.value = null
  try {
    const accountId = filters.value.account_id.trim()
    const [healthResult, runtimeResult, positionResult, activeOrdersResult, tradeResult, dailyResult, pnlResult] = await Promise.allSettled([
      operationsApi.health(),
      operationsApi.runtimeStatus({ ...query.value, limit: 100 }),
      operationsApi.positions({ ...query.value, limit: 1 }),
      operationsApi.orders({ ...query.value, active_only: true, limit: 1 }),
      operationsApi.trades({ ...query.value, limit: 6 }),
      operationsApi.dailyPnl({
        ...query.value,
        start_date: startDate,
        end_date: endDate,
        timezone: 'Asia/Shanghai'
      }),
      accountId
        ? operationsApi.pnl({ account_id: accountId, ...query.value })
        : Promise.resolve(null)
    ])
    unavailableSources.value = []
    health.value = healthResult.status === 'fulfilled' ? healthResult.value : null
    if (healthResult.status === 'rejected') unavailableSources.value.push('账本健康')
    runtimes.value = runtimeResult.status === 'fulfilled' ? runtimeResult.value.items : []
    if (runtimeResult.status === 'rejected') unavailableSources.value.push('策略心跳')
    positionsTotal.value = positionResult.status === 'fulfilled' ? positionResult.value.total : null
    if (positionResult.status === 'rejected') unavailableSources.value.push('当前持仓')
    activeOrdersTotal.value = activeOrdersResult.status === 'fulfilled' ? activeOrdersResult.value.total : null
    if (activeOrdersResult.status === 'rejected') unavailableSources.value.push('活动订单')
    recentTrades.value = tradeResult.status === 'fulfilled' ? tradeResult.value.items : []
    if (tradeResult.status === 'rejected') unavailableSources.value.push('最近成交')
    daily.value = dailyResult.status === 'fulfilled' ? dailyResult.value : []
    if (dailyResult.status === 'rejected') unavailableSources.value.push('当月已实现收益')
    pnl.value = pnlResult.status === 'fulfilled' ? pnlResult.value : null
    if (accountId && pnlResult.status === 'rejected') unavailableSources.value.push('当前浮动收益')
    if ([healthResult, runtimeResult, positionResult, activeOrdersResult, tradeResult, dailyResult].every((item) => item.status === 'rejected')) {
      throw new Error('运行数据接口均不可用')
    }
    refreshedAt.value = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date())
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '运行总览加载失败'
  } finally {
    loading.value = false
  }
}

async function applyFilters() {
  await router.replace({ query: { ...query.value } })
  await load()
}

function openDay(date: string) {
  void router.push({ path: '/trades', query: { ...query.value, date } })
}

onMounted(load)
</script>

<template>
  <main class="operations-page overview-page">
    <PageHeader
      eyebrow="OPERATIONS / LIVE LEDGER"
      title="运行总览"
      description="把账本健康、策略心跳、执行门禁与当月已实现收益放在同一条运行链路上。"
      :loading="loading"
      :refreshed-at="refreshedAt"
      @refresh="load"
    >
      <template #actions>
        <a-tag v-for="mode in runtimeModes" :key="mode" :color="mode === 'live' ? 'red' : 'gold'">{{ mode.toUpperCase() }}</a-tag>
        <a-tag v-if="!runtimeModes.length">环境未知</a-tag>
      </template>
    </PageHeader>

    <FilterBar v-model="filters" @apply="applyFilters" />
    <DataState :loading="loading" :error="error" @retry="load">
      <a-alert v-if="unavailableSources.length" type="warning" show-icon :message="`部分数据读取失败：${unavailableSources.join('、')}`" description="失败项显示为“读取失败”，不会当作 0 或空数据。" class="partial-failure" />
      <section class="health-rail" aria-label="运行健康状态">
        <article :class="['health-node', health?.status === 'healthy' ? 'ok' : 'bad']">
          <Database :size="18" /><div><span>账本数据库</span><strong>{{ health?.status === 'healthy' ? '正常' : '不可用' }}</strong></div>
        </article>
        <article :class="['health-node', unhealthyRuntimes.length ? 'bad' : runtimes.length ? 'ok' : 'unknown']">
          <Activity :size="18" /><div><span>策略运行</span><strong>{{ runtimes.length ? `${runtimes.length - unhealthyRuntimes.length}/${runtimes.length} 正常` : '无心跳数据' }}</strong></div>
        </article>
        <article class="health-node unknown">
          <RadioTower :size="18" /><div><span>行情连续性</span><strong>尚无独立查询接口</strong></div>
        </article>
        <article :class="['health-node', blockedRuntimes.length ? 'bad' : runtimes.length ? 'ok' : 'unknown']">
          <ShieldCheck :size="18" /><div><span>执行门禁</span><strong>{{ runtimes.length ? (blockedRuntimes.length ? `${blockedRuntimes.length} 个受限` : '允许入场') : '无运行数据' }}</strong></div>
        </article>
      </section>

      <section class="metric-grid overview-metrics">
        <MetricTile label="今日闭合净 PnL" :value="formatMoney(todayPnl?.net_pnl)" :hint="`Campaign closed_at 上海日界线 · ${closedPnlScope}`" :tone="todayPnl?.net_pnl == null ? 'neutral' : asNumber(todayPnl.net_pnl) >= 0 ? 'positive' : 'negative'" :to="{ path: '/calendar', query }" />
        <MetricTile label="当前浮动 PnL" :value="filters.account_id.trim() ? formatMoney(pnl?.total_unrealized_pnl) : '—'" :hint="filters.account_id.trim() ? '来自当前筛选账户' : '选择账户后查看当前浮动收益'" :tone="pnl?.total_unrealized_pnl == null ? 'neutral' : asNumber(pnl.total_unrealized_pnl) >= 0 ? 'positive' : 'negative'" :to="{ path: '/positions', query: { ...query, tab: 'positions' } }" />
        <MetricTile label="当月闭合净 PnL" :value="formatMoney(monthNet)" :hint="monthNet == null ? '存在无法统一计价的日期' : `${winningDays} 盈利日 / ${losingDays} 亏损日`" :tone="monthNet == null ? 'warning' : monthNet >= 0 ? 'positive' : 'negative'" :to="{ path: filters.account_id.trim() ? '/performance' : '/calendar', query }" />
        <MetricTile label="当前持仓" :value="positionsTotal == null ? '读取失败' : String(positionsTotal)" hint="进入持仓明细" :tone="positionsTotal == null ? 'warning' : 'neutral'" :to="{ path: '/positions', query: { ...query, tab: 'positions' } }" />
        <MetricTile label="活动订单" :value="activeOrdersTotal == null ? '读取失败' : String(activeOrdersTotal)" hint="NEW + PARTIALLY_FILLED" :tone="activeOrdersTotal == null || activeOrdersTotal > 0 ? 'warning' : 'neutral'" :to="{ path: '/positions', query: { ...query, tab: 'active' } }" />
      </section>

      <section class="two-column overview-panels">
        <article class="data-card calendar-card">
          <div class="data-card-heading">
            <div><h2><CalendarDays :size="15" /> 收益日历</h2><p>{{ year }} 年 {{ month }} 月 · {{ closedPnlScope }} · 日界线 Asia/Shanghai</p></div>
            <RouterLink :to="{ path: '/calendar', query: query }" class="table-link">查看完整日历 →</RouterLink>
          </div>
          <PnlCalendar :year="year" :month="month" :rows="daily" compact @day="openDay" />
          <div v-if="!daily.length" class="inline-empty">{{ unavailableSources.includes('当月已实现收益') ? '收益日历读取失败' : '本月没有已实现收益记录' }}</div>
        </article>

        <article class="data-card recent-card">
          <div class="data-card-heading"><div><h2>最近成交</h2><p>按账本返回顺序，最多 6 条</p></div><RouterLink :to="{ path: '/trades', query: query }" class="table-link">全部成交 →</RouterLink></div>
          <div v-if="recentTrades.length" class="recent-list">
            <button v-for="trade in recentTrades" :key="trade.id" type="button" @click="router.push({ path: '/trades', query: { ...query, campaign_id: trade.campaign_id || undefined } })">
              <div><strong>{{ trade.symbol }}</strong><span>{{ trade.side }} · {{ trade.quantity }}</span></div>
              <div><strong :class="pnlClass(trade.realized_pnl)">{{ formatMoney(trade.realized_pnl) }}</strong><time>{{ formatDateTime(trade.exchange_time) }}</time></div>
            </button>
          </div>
          <div v-else class="inline-empty"><CircleAlert :size="16" /> 当前筛选条件下没有成交</div>
        </article>
      </section>
    </DataState>
  </main>
</template>

<style scoped lang="scss">
.health-rail { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-bottom:12px; }
.health-node { display:flex; align-items:center; gap:10px; min-height:66px; padding:10px 12px; border:1px solid var(--line); border-left:3px solid #7b8997; border-radius:5px; background:var(--surface); }
.health-node.ok { border-left-color:#2f9d72; }.health-node.bad { border-left-color:#bd625b; }.health-node.unknown { border-left-color:#c08a32; }
.health-node svg { flex:0 0 auto; color:var(--muted); }.health-node span,.health-node strong { display:block; }.health-node span { color:var(--muted); font-size:10px; }.health-node strong { margin-top:4px; font-size:12px; }
.partial-failure { margin-bottom:12px; }.overview-metrics { margin-bottom:14px; }.overview-panels { margin-top:4px; }
.data-card-heading h2 { display:flex; align-items:center; gap:6px; }
.recent-list button { display:flex; align-items:center; justify-content:space-between; gap:12px; width:100%; min-height:59px; padding:9px 13px; border:0; border-bottom:1px solid var(--line); background:transparent; color:var(--text); text-align:left; cursor:pointer; }
.recent-list button:last-child { border-bottom:0; }.recent-list button:hover { background:var(--surface-hover); }.recent-list button > div:last-child { text-align:right; }
.recent-list strong,.recent-list span,.recent-list time { display:block; }.recent-list strong { font:12px "IBM Plex Mono",monospace; }.recent-list span,.recent-list time { margin-top:3px; color:var(--muted); font-size:10px; }
.inline-empty { display:flex; align-items:center; justify-content:center; gap:7px; min-height:100px; padding:12px; color:var(--muted); font-size:11px; }
.calendar-card .inline-empty { min-height:auto; padding:0 12px 12px; }
@media (max-width: 1000px) { .health-rail { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (max-width: 560px) { .health-rail { grid-template-columns:1fr; } }
</style>
