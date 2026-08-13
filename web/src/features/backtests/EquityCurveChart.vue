<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { AreaSeries, ColorType, createChart, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp } from 'lightweight-charts'
import type { EquityPoint, EquityReplayRow } from './equityReplay'
import { formatNumber, formatPercent, formatTime } from './format'

const props = defineProps<{ points: EquityPoint[] }>()
const host = ref<HTMLElement | null>(null)
const hovered = ref<EquityReplayRow | null>(null)
const isDarkTheme = inject<Ref<boolean>>('isDarkTheme', ref(false))
let chart: IChartApi | null = null
let series: ISeriesApi<'Area'> | null = null
let observer: ResizeObserver | null = null

const pointRows = computed(() => new Map(
  props.points.filter((point) => point.row).map((point) => [Math.floor(point.time / 1000), point.row!])
))

function render() {
  if (!host.value) return
  chart?.remove()
  const dark = isDarkTheme.value
  chart = createChart(host.value, {
    width: host.value.clientWidth,
    height: host.value.clientHeight,
    layout: { background: { type: ColorType.Solid, color: dark ? '#111827' : '#ffffff' }, textColor: dark ? '#94a8c1' : '#64748b' },
    grid: { vertLines: { color: dark ? '#263243' : '#edf1f6' }, horzLines: { color: dark ? '#263243' : '#edf1f6' } },
    rightPriceScale: { borderColor: dark ? '#41536b' : '#cbd5e1' },
    timeScale: { borderColor: dark ? '#41536b' : '#cbd5e1', timeVisible: true, secondsVisible: false },
    localization: { priceFormatter: (value: number) => `${formatNumber(value, 2)} U` }
  })
  series = chart.addSeries(AreaSeries, {
    lineColor: '#16a34a', topColor: 'rgba(22, 163, 74, .28)', bottomColor: 'rgba(22, 163, 74, .02)', lineWidth: 2,
    crosshairMarkerVisible: true, crosshairMarkerRadius: 5, priceLineVisible: false
  })
  series.setData(props.points.map((point) => ({ time: Math.floor(point.time / 1000) as UTCTimestamp, value: point.value })))
  chart.subscribeCrosshairMove((param) => {
    if (!param.time) { hovered.value = null; return }
    hovered.value = pointRows.value.get(Number(param.time as Time)) ?? null
  })
  chart.timeScale().fitContent()
}

watch(() => props.points, () => nextTick(render), { deep: true })
watch(isDarkTheme, () => nextTick(render))
onMounted(() => {
  render()
  observer = new ResizeObserver(() => chart?.applyOptions({ width: host.value?.clientWidth ?? 0, height: host.value?.clientHeight ?? 0 }))
  if (host.value) observer.observe(host.value)
})
onBeforeUnmount(() => { observer?.disconnect(); chart?.remove() })
</script>

<template>
  <div class="equity-chart-wrap">
    <div ref="host" class="equity-chart-host" />
    <div v-if="hovered" class="equity-tooltip">
      <div><strong>{{ hovered.symbol }}</strong><span>{{ hovered.side || '-' }}</span></div>
      <dl>
        <dt>结算时间</dt><dd>{{ formatTime(hovered.exit_time) }}</dd>
        <dt>入场 / 退出</dt><dd>{{ formatNumber(hovered.entry_price, 8) }} / {{ formatNumber(hovered.exit_price, 8) }}</dd>
        <dt>动态仓位</dt><dd>{{ formatNumber(hovered.positionAmount) }} U</dd>
        <dt>手续费</dt><dd>{{ formatNumber(hovered.feeAmount) }} U</dd>
        <dt>滑点影响</dt><dd>{{ formatNumber(hovered.slippageAmount) }} U</dd>
        <dt>单笔收益率</dt><dd>{{ formatPercent(hovered.netReturn) }}</dd>
        <dt>单笔盈亏</dt><dd :class="hovered.replayPnl >= 0 ? 'value-positive' : 'value-negative'">{{ formatNumber(hovered.replayPnl) }} U</dd>
        <dt>结算后权益</dt><dd>{{ formatNumber(hovered.balanceAfter) }} U</dd>
      </dl>
    </div>
  </div>
</template>
