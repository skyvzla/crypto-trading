<script lang="ts">
import type { UTCTimestamp } from 'lightweight-charts'
import { deduplicateEquityPoints, type EquityPoint } from './equityReplay'

export interface EquitySeriesPoint {
  time: UTCTimestamp
  value: number
}

/**
 * 回放点 → lightweight-charts 的 (秒级时间戳, 权益) 序列。
 *
 * v5 的 setData 会执行 checkItemsAreOrdered(..., allowDuplicates = false)，
 * 只要出现重复时间就直接抛 “data must be asc ordered by time”。回放数据并不保证
 * 时间唯一：策略存在 0 秒持仓（entry_time == exit_time），replayEquity 也只保证
 * entryTime >= activeUntil，同一秒里完全可能落进两笔结算。
 *
 * 所以这里先按秒升序去重（同一秒保留最后一个点，规则见 deduplicateEquityPoints），
 * 再映射成图表序列。去重只在同一秒内折叠，不会丢秒：被折叠掉的是该秒的中间状态，
 * 保留最后一个点既符合“后写的是更新状态”，也和 tooltip 的秒级索引一致。
 * 函数是幂等的，调用方预先去过重也不会重复处理。
 *
 * 之所以导出成独立函数，是为了不挂载组件就能覆盖这条数据契约。
 */
export function toEquitySeriesPoints(points: EquityPoint[]): EquitySeriesPoint[] {
  return deduplicateEquityPoints(points).map((point) => ({
    time: Math.floor(point.time / 1000) as UTCTimestamp,
    value: point.value,
  }))
}
</script>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AreaSeries, ColorType, createChart, type IChartApi, type Time } from 'lightweight-charts'
import type { EquityReplayRow } from './equityReplay'
import { formatDateTime, formatNumber, formatPercent } from '@/shared/format'
import { IS_DARK_THEME } from '@/shared/theme'
import { getChartTheme } from './chartTheme'

const props = defineProps<{ points: EquityPoint[] }>()
const host = ref<HTMLElement | null>(null)
const hovered = ref<EquityReplayRow | null>(null)
/** setData 失败时的可见提示；只写控制台的话用户只会看到一块空白。 */
const renderError = ref<string | null>(null)
const isDarkTheme = inject(
  IS_DARK_THEME,
  computed(() => false),
)
let chart: IChartApi | null = null
/** 当前图表自己的一层容器，替换图表时整层换掉，不在宿主节点里留残留。 */
let chartContainer: HTMLElement | null = null
let observer: ResizeObserver | null = null

const seriesPoints = computed(() => toEquitySeriesPoints(props.points))
/**
 * tooltip 按秒取行；同一秒出现多条时后者覆盖前者，与图表“保留最后一个点”一致。
 * 这里不做去重是因为 Map 本身就是后写覆盖，且查询不依赖顺序。
 */
const pointRows = computed(
  () => new Map(props.points.filter((point) => point.row).map((point) => [Math.floor(point.time / 1000), point.row!])),
)

function describeError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return String(error)
}

function disposeChart() {
  // chart.remove() 会连同它自己的序列一起销毁，不需要单独保留 series 句柄。
  chart?.remove()
  chart = null
  chartContainer?.remove()
  chartContainer = null
}

/**
 * 先把新图建在独立容器里并写入数据，只有 setData 成功才替换正在显示的图表。
 *
 * 图表库对非法数据（重复时间、非有限数值等）会直接抛错；如果沿用先 remove()
 * 旧图再建新图的顺序，一次脏数据就会让整条权益曲线变成空白、异常直接冒到全局。
 * 失败路径只销毁新容器，屏幕上留下的仍是上一份可用数据。
 */
function render() {
  const hostElement = host.value
  if (!hostElement) return
  const palette = getChartTheme(isDarkTheme.value)
  const container = document.createElement('div')
  // 画布按显式 width/height 渲染，这一层只负责铺满宿主节点。
  container.className = 'equity-chart-canvas'
  container.style.position = 'absolute'
  container.style.inset = '0'
  const nextChart = createChart(container, {
    width: hostElement.clientWidth,
    height: hostElement.clientHeight,
    layout: { background: { type: ColorType.Solid, color: palette.background }, textColor: palette.axisText },
    grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
    rightPriceScale: { borderColor: palette.border },
    timeScale: { borderColor: palette.border, timeVisible: true, secondsVisible: false },
    localization: { priceFormatter: (value: number) => `${formatNumber(value, 2)} U` },
  })
  const nextSeries = nextChart.addSeries(AreaSeries, {
    lineColor: palette.areaLine,
    topColor: palette.areaTop,
    bottomColor: palette.areaBottom,
    lineWidth: 2,
    crosshairMarkerVisible: true,
    crosshairMarkerRadius: 5,
    priceLineVisible: false,
  })
  try {
    nextSeries.setData(seriesPoints.value)
  } catch (error) {
    nextChart.remove()
    renderError.value = `权益曲线绘制失败，已保留上一份数据：${describeError(error)}`
    return
  }

  disposeChart()
  chartContainer = container
  hostElement.appendChild(container)
  chart = nextChart
  renderError.value = null
  chart.subscribeCrosshairMove((param) => {
    if (!param.time) {
      hovered.value = null
      return
    }
    hovered.value = pointRows.value.get(Number(param.time as Time)) ?? null
  })
  chart.timeScale().fitContent()
}

watch(
  () => props.points,
  () => nextTick(render),
)
watch(isDarkTheme, () => nextTick(render))
onMounted(() => {
  render()
  observer = new ResizeObserver(() =>
    chart?.applyOptions({ width: host.value?.clientWidth ?? 0, height: host.value?.clientHeight ?? 0 }),
  )
  if (host.value) observer.observe(host.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  disposeChart()
})
</script>

<template>
  <div class="equity-chart-wrap">
    <div ref="host" class="equity-chart-host" />
    <p v-if="renderError" class="status-strip error equity-chart-error" role="alert">{{ renderError }}</p>
    <div v-if="hovered" class="equity-tooltip">
      <div>
        <strong>{{ hovered.symbol }}</strong
        ><span>{{ hovered.side || '-' }}</span>
      </div>
      <dl>
        <dt>结算时间</dt>
        <dd>{{ formatDateTime(hovered.exit_time) }}</dd>
        <dt>入场 / 退出</dt>
        <dd>{{ formatNumber(hovered.entry_price, 8) }} / {{ formatNumber(hovered.exit_price, 8) }}</dd>
        <dt>本笔仓位</dt>
        <dd>{{ formatNumber(hovered.positionAmount) }} U</dd>
        <dt>手续费</dt>
        <dd>{{ formatNumber(hovered.feeAmount) }} U</dd>
        <dt>滑点影响</dt>
        <dd>{{ formatNumber(hovered.slippageAmount) }} U</dd>
        <dt>单笔收益率</dt>
        <dd>{{ formatPercent(hovered.netReturn) }}</dd>
        <dt>单笔盈亏</dt>
        <!-- prettier-ignore -->
        <dd :class="hovered.replayPnl >= 0 ? 'value-positive' : 'value-negative'">{{ formatNumber(hovered.replayPnl) }} U</dd>
        <dt>本笔复投</dt>
        <dd>{{ formatNumber(hovered.reinvestedProfit) }} U</dd>
        <dt>交易资金池</dt>
        <dd>{{ formatNumber(hovered.tradingCapitalAfter) }} U</dd>
        <dt>锁定储备</dt>
        <dd>{{ formatNumber(hovered.reserveCapitalAfter) }} U</dd>
        <dt>结算后权益</dt>
        <dd>{{ formatNumber(hovered.balanceAfter) }} U</dd>
      </dl>
    </div>
  </div>
</template>

<style scoped>
.equity-chart-error {
  position: absolute;
  z-index: 4;
  right: 14px;
  bottom: 14px;
  left: 14px;
}
</style>
