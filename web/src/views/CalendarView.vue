<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronLeft, ChevronRight, Clock3 } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { DailyPnL } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import FilterBar from '@/features/operations/FilterBar.vue'
import MetricTile from '@/features/operations/MetricTile.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import PnlCalendar from '@/features/operations/PnlCalendar.vue'
import {
  isQuerySynced,
  useLedgerLoader,
  useOperationFilters,
  useQuerySync,
} from '@/features/operations/useOperationsView'
import { asNumber, formatMoney } from '@/shared/format'
import { LEDGER_TIMEZONE, ledgerMonth, ledgerMonthRange, shiftLedgerMonth } from '@/shared/time'

const route = useRoute()
const router = useRouter()
const syncQuery = useQuerySync()
const { filters, query: filtersQuery, restore: restoreFilters } = useOperationFilters()

function readMonth(): string {
  const requested = String(route.query.month ?? '')
  return /^\d{4}-\d{2}$/.test(requested) ? requested : ledgerMonth()
}

const initial = readMonth()
const year = ref(Number(initial.slice(0, 4)))
const month = ref(Number(initial.slice(5, 7)))
const rows = ref<DailyPnL[]>([])

const monthRange = computed(() => ledgerMonthRange(year.value, month.value))
const startDate = computed(() => monthRange.value.startDate)
const endDate = computed(() => monthRange.value.endDate)
const monthKey = computed(() => `${year.value}-${String(month.value).padStart(2, '0')}`)
const monthNetAvailable = computed(() => rows.value.every((item) => item.net_pnl != null))
const monthNet = computed(() =>
  monthNetAvailable.value ? rows.value.reduce((sum, item) => sum + asNumber(item.net_pnl), 0) : null,
)
const wins = computed(() => rows.value.filter((item) => asNumber(item.net_pnl) > 0).length)
const losses = computed(() => rows.value.filter((item) => asNumber(item.net_pnl) < 0).length)
const campaignCount = computed(() => rows.value.reduce((sum, item) => sum + item.campaign_count, 0))
const fillCount = computed(() => rows.value.reduce((sum, item) => sum + item.fill_count, 0))

/** 本页放进地址栏的内容。写回与「URL 是否被外部改动」共用这一处声明。 */
function routeQuery() {
  return { ...filtersQuery.value, month: monthKey.value }
}

/** 把地址栏状态同步回本地 ref。 */
function restoreFromRoute() {
  restoreFilters()
  const restored = readMonth()
  year.value = Number(restored.slice(0, 4))
  month.value = Number(restored.slice(5, 7))
}

const { loading, error, refreshedAt, reload } = useLedgerLoader(
  async ({ isStale }) => {
    const result = await operationsApi.dailyPnl({
      ...filtersQuery.value,
      start_date: startDate.value,
      end_date: endDate.value,
      timezone: LEDGER_TIMEZONE,
    })
    if (isStale()) return
    rows.value = result
  },
  {
    fallbackMessage: '收益日历加载失败',
    onActivate: restoreFromRoute,
  },
)

// 已经在本页时直接改地址栏——手改 URL、打开一条带不同筛选的分享链接——组件
// 既不会重新挂载也不会重新 activate，只靠 onActivated 跟不上。
//
// 自己写回的 query 与 routeQuery() 一致，所以这里不会把应用筛选变成两次请求；
// 路由名变了说明已经切走，被缓存的实例不该再管地址栏。
const ownRoute = route.name
watch(
  () => route.query,
  () => {
    if (route.name !== ownRoute || isQuerySynced(route.query, routeQuery())) return
    restoreFromRoute()
    void reload()
  },
)

async function syncAndLoad() {
  await syncQuery(routeQuery())
  await reload()
}

async function moveMonth(delta: number) {
  const next = shiftLedgerMonth(year.value, month.value, delta)
  year.value = next.year
  month.value = next.month
  await syncAndLoad()
}

function openDay(date: string) {
  void router.push({ path: '/trades', query: { ...filtersQuery.value, date } })
}
</script>

<template>
  <main class="operations-page calendar-page">
    <PageHeader
      eyebrow="OPERATIONS / DAILY PNL"
      title="收益日历"
      description="按 Asia/Shanghai 自然日查看已实现净收益；收益为空与接口异常明确区分。"
      :loading="loading"
      :refreshed-at="refreshedAt"
      @refresh="reload"
    >
      <template #actions
        ><a-button @click="router.push({ path: '/overview', query: filtersQuery })">返回运行总览</a-button></template
      >
    </PageHeader>
    <FilterBar v-model="filters" @apply="syncAndLoad" />
    <div class="calendar-toolbar">
      <a-button aria-label="上个月" @click="moveMonth(-1)"
        ><template #icon><ChevronLeft :size="15" /></template></a-button
      ><strong>{{ year }} 年 {{ month }} 月</strong
      ><a-button aria-label="下个月" @click="moveMonth(1)"
        ><template #icon><ChevronRight :size="15" /></template></a-button
      ><span><Clock3 :size="13" /> 日界线 Asia/Shanghai</span>
    </div>
    <section class="metric-grid calendar-metrics">
      <MetricTile
        label="本月累计净 PnL"
        :value="formatMoney(monthNet)"
        :tone="monthNet == null ? 'warning' : monthNet >= 0 ? 'positive' : 'negative'"
        :hint="monthNet == null ? '存在无法统一计价的日期' : '按闭合 Campaign 日净额求和'"
      />
      <MetricTile label="盈利日" :value="String(wins)" tone="positive" hint="净 PnL > 0" />
      <MetricTile label="亏损日" :value="String(losses)" tone="negative" hint="净 PnL < 0" />
      <MetricTile label="闭合 Campaign" :value="String(campaignCount)" :hint="`${fillCount} 笔关联 fills`" />
    </section>
    <div class="status-strip">
      <Clock3 :size="13" /><span
        >完整且已闭合的 Campaign 按 <strong>closed_at 上海自然日</strong>归属 {{ startDate }} →
        {{ endDate }}；账户为空时由服务端汇总全部账户，净额仅扣同币种 USDT 手续费，资金费尚无权威事实。</span
      >
    </div>
    <DataState :loading="loading" :error="error" @retry="reload">
      <section class="data-card full-calendar">
        <PnlCalendar :year="year" :month="month" :rows="rows" @day="openDay" />
        <div v-if="!rows.length" class="calendar-empty">本月没有已实现收益记录；点击任意日期仍可进入成交复盘。</div>
      </section>
    </DataState>
  </main>
</template>

<style scoped lang="scss">
.calendar-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 2px 0 12px;
}
.calendar-toolbar strong {
  min-width: 120px;
  text-align: center;
  font: var(--type-primary) var(--font-family-mono);
}
.calendar-toolbar > span {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-left: auto;
  color: var(--color-gold);
  font: var(--type-meta) var(--font-family-mono);
}
.calendar-metrics {
  margin-bottom: 10px;
}
.status-strip {
  margin-bottom: 10px;
}
.full-calendar {
  margin-top: 2px;
}
.calendar-empty {
  padding: 0 14px 14px;
  color: var(--muted);
  text-align: center;
  font-size: var(--type-meta);
}
@media (max-width: 540px) {
  .calendar-toolbar > span {
    width: 100%;
    margin: 4px 0 0;
  }
  .calendar-toolbar {
    flex-wrap: wrap;
  }
}
</style>
