<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { Activity, CalendarDays, CircleAlert, Database, RadioTower, ShieldCheck, WalletCards } from 'lucide-vue-next'
import { ApiError } from '@/api/client'
import { operationsApi } from '@/api/operations'
import type { DailyPnL, Health, LedgerTrade, PnLSummary, StrategyCapitalStatus, StrategyRuntimeStatus } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import { campaignRoute } from '@/features/operations/campaignRoute'
import FilterBar, { type OperationFilters } from '@/features/operations/FilterBar.vue'
import MetricTile from '@/features/operations/MetricTile.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import PnlCalendar from '@/features/operations/PnlCalendar.vue'
import { asNumber, formatDateTime, formatMoney, formatPercent, pnlClass } from '@/features/operations/format'

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
const capital = ref<StrategyCapitalStatus | null>(null)
const capitalNotFound = ref(false)
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
const capitalGate = computed(() => {
  if (!capital.value) return null
  if (capital.value.capital_breached) return { label: '资金越界', color: 'red' }
  if (asNumber(capital.value.trading_capital) <= asNumber(capital.value.minimum)) {
    return { label: '停止开仓', color: 'gold' }
  }
  return { label: '允许开仓', color: 'green' }
})

async function load() {
  loading.value = true
  error.value = null
  try {
    const accountId = filters.value.account_id.trim()
    const strategyId = filters.value.strategy_id.trim()
    const [healthResult, runtimeResult, positionResult, activeOrdersResult, tradeResult, dailyResult, pnlResult, capitalResult] = await Promise.allSettled([
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
        : Promise.resolve(null),
      accountId && strategyId
        ? operationsApi.capitalStatus({ account_id: accountId, strategy_id: strategyId })
        : Promise.resolve(null)
    ])
    unavailableSources.value = []
    capitalNotFound.value = false
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
    capital.value = capitalResult.status === 'fulfilled' ? capitalResult.value : null
    if (accountId && strategyId && capitalResult.status === 'rejected') {
      if (capitalResult.reason instanceof ApiError && capitalResult.reason.status === 404) {
        capitalNotFound.value = true
      } else {
        unavailableSources.value.push('策略资金')
      }
    }
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

function openRecentTrade(trade: LedgerTrade) {
  const target = campaignRoute(trade)
  void router.push(target || { name: 'trades', query: { ...query.value } })
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

      <section class="capital-card" aria-label="策略资金状态">
        <header class="capital-heading">
          <div class="capital-title">
            <WalletCards :size="19" />
            <div>
              <h2>策略资金状态</h2>
              <p v-if="capital"><span>{{ capital.account_id }}</span> / <span>{{ capital.strategy_id }}</span></p>
              <p v-else>账户级交易池与储备池</p>
            </div>
          </div>
          <a-tag v-if="capitalGate" :color="capitalGate.color">{{ capitalGate.label }}</a-tag>
        </header>
        <div v-if="capital" class="capital-grid">
          <div class="capital-primary"><span>可交易资金</span><strong>{{ formatMoney(capital.trading_capital) }} <small>USDT</small></strong></div>
          <div><span>账户资金</span><strong>{{ formatMoney(capital.account_capital) }} <small>USDT</small></strong></div>
          <div><span>储备资金</span><strong>{{ formatMoney(capital.reserve_capital) }} <small>USDT</small></strong></div>
          <div><span>停止开仓阈值</span><strong>{{ formatMoney(capital.minimum) }} <small>USDT</small></strong></div>
          <div><span>盈利复投比例</span><strong>{{ formatPercent(capital.profit_reinvest_ratio, 0) }}</strong></div>
          <div><span>资金越界</span><strong :class="capital.capital_breached ? 'value-negative' : 'value-positive'">{{ capital.capital_breached ? '是' : '否' }}</strong></div>
          <div><span>状态版本</span><strong>v{{ capital.version }}</strong></div>
          <div><span>更新时间</span><strong class="capital-time">{{ formatDateTime(capital.updated_at) }}</strong></div>
        </div>
        <div v-else class="capital-empty">
          <CircleAlert :size="16" />
          <span v-if="capitalNotFound">此账户与策略尚未初始化资金状态</span>
          <span v-else-if="unavailableSources.includes('策略资金')">策略资金状态读取失败</span>
          <span v-else>选择账户和策略后查看资金状态</span>
        </div>
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
            <button v-for="trade in recentTrades" :key="trade.id" type="button" @click="openRecentTrade(trade)">
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
.health-node { display:flex; align-items:center; gap:10px; min-height:66px; padding:10px 12px; border:1px solid var(--line); border-left:3px solid var(--muted); border-radius:5px; background:var(--surface); }
.health-node.ok { border-left-color:var(--color-success); }.health-node.bad { border-left-color:var(--color-danger); }.health-node.unknown { border-left-color:var(--color-warning); }
.health-node svg { flex:0 0 auto; color:var(--muted); }.health-node span,.health-node strong { display:block; }.health-node span { color:var(--muted); font-size:var(--font-size-xs); }.health-node strong { margin-top:4px; font-size:var(--font-size-sm); }
.partial-failure { margin-bottom:12px; }.overview-metrics { margin-bottom:14px; }.overview-panels { margin-top:4px; }
.capital-card { margin-bottom:14px; overflow:hidden; border:1px solid var(--line); border-left:3px solid var(--color-gold); border-radius:6px; background:var(--surface); }
.capital-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:58px; padding:10px 14px; border-bottom:1px solid var(--line); }
.capital-title { display:flex; align-items:center; gap:9px; min-width:0; }.capital-title > svg { flex:0 0 auto; color:var(--color-gold); }
.capital-title h2 { margin:0; font-size:var(--font-size-md); }.capital-title p { margin:3px 0 0; color:var(--muted); font:var(--font-size-xs)/1.35 var(--font-family-mono); overflow-wrap:anywhere; }
.capital-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); }
.capital-grid > div { min-width:0; padding:12px 14px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); }
.capital-grid > div:nth-child(4n) { border-right:0; }.capital-grid > div:nth-last-child(-n+4) { border-bottom:0; }.capital-grid > .capital-primary { background:var(--surface-hover); }
.capital-grid span,.capital-grid strong { display:block; }.capital-grid span { margin-bottom:5px; color:var(--muted); font-size:var(--font-size-xs); }
.capital-grid strong { overflow-wrap:anywhere; font:600 var(--font-size-md)/1.3 var(--font-family-mono); }.capital-grid small { color:var(--muted); font-size:10px; font-weight:500; }
.capital-grid .capital-time { font-size:var(--font-size-xs); line-height:1.45; }.capital-empty { display:flex; align-items:center; justify-content:center; gap:7px; min-height:82px; padding:14px; color:var(--muted); font-size:var(--font-size-sm); }
.data-card-heading h2 { display:flex; align-items:center; gap:6px; }
.recent-list button { display:flex; align-items:center; justify-content:space-between; gap:12px; width:100%; min-height:59px; padding:9px 13px; border:0; border-bottom:1px solid var(--line); background:transparent; color:var(--text); text-align:left; cursor:pointer; }
.recent-list button:last-child { border-bottom:0; }.recent-list button:hover { background:var(--surface-hover); }.recent-list button > div:last-child { text-align:right; }
.recent-list strong,.recent-list span,.recent-list time { display:block; }.recent-list strong { font:var(--font-size-sm) var(--font-family-mono); }.recent-list span,.recent-list time { margin-top:3px; color:var(--muted); font-size:var(--font-size-xs); }
.inline-empty { display:flex; align-items:center; justify-content:center; gap:7px; min-height:100px; padding:12px; color:var(--muted); font-size:var(--font-size-xs); }
.calendar-card .inline-empty { min-height:auto; padding:0 12px 12px; }
@media (max-width: 1000px) { .health-rail,.capital-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }.capital-grid > div { border-bottom:1px solid var(--line); }.capital-grid > div:nth-child(2n) { border-right:0; }.capital-grid > div:nth-last-child(-n+2) { border-bottom:0; } }
@media (max-width: 560px) { .health-rail { grid-template-columns:1fr; }.capital-grid { grid-template-columns:1fr; }.capital-grid > div { border-right:0; border-bottom:1px solid var(--line) !important; }.capital-grid > div:last-child { border-bottom:0 !important; } }
</style>
