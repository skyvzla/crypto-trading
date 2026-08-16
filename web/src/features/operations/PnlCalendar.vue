<script setup lang="ts">
import { computed } from 'vue'
import { asNumber, formatMoney, pnlClass } from './format'

interface DailyPnlRow {
  date: string
  net_pnl: string | number | null
  campaign_count: number
  fill_count: number
}

const props = withDefaults(defineProps<{
  year: number
  month: number
  rows: DailyPnlRow[]
  compact?: boolean
}>(), { compact: false })

const emit = defineEmits<{ day: [date: string] }>()

const weekdays = ['一', '二', '三', '四', '五', '六', '日']
const cells = computed(() => {
  const firstDay = new Date(Date.UTC(props.year, props.month - 1, 1))
  const days = new Date(Date.UTC(props.year, props.month, 0)).getUTCDate()
  const leading = (firstDay.getUTCDay() + 6) % 7
  const byDate = new Map(props.rows.map((row) => [row.date, row]))
  const result: Array<{ date: string; day: number; row?: DailyPnlRow } | null> = []
  for (let index = 0; index < leading; index += 1) result.push(null)
  for (let day = 1; day <= days; day += 1) {
    const date = `${props.year}-${String(props.month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
    result.push({ date, day, row: byDate.get(date) })
  }
  return result
})

function intensity(row?: DailyPnlRow): number {
  if (!row || row.net_pnl == null) return 0
  const max = Math.max(1, ...props.rows.filter((item) => item.net_pnl != null).map((item) => Math.abs(asNumber(item.net_pnl))))
  return Math.max(.12, Math.min(.58, Math.abs(asNumber(row.net_pnl)) / max * .58))
}
</script>

<template>
  <div class="pnl-calendar" :class="{ compact }">
    <div v-for="weekday in weekdays" :key="weekday" class="calendar-weekday">{{ weekday }}</div>
    <template v-for="(cell, index) in cells" :key="cell?.date || `blank-${index}`">
      <div v-if="!cell" class="calendar-cell blank" />
      <button
        v-else
        type="button"
        class="calendar-cell"
        :class="cell.row ? (cell.row.net_pnl == null ? 'net-unavailable' : pnlClass(cell.row.net_pnl)) : 'no-data'"
        :style="cell.row?.net_pnl != null ? { '--cell-alpha': intensity(cell.row) } : undefined"
        :aria-label="`${cell.date}${cell.row ? (cell.row.net_pnl == null ? ' 净收益不可用' : ` 净收益 ${formatMoney(cell.row.net_pnl)}`) : ' 无数据'}`"
        @click="emit('day', cell.date)"
      >
        <span class="calendar-day">{{ cell.day }}</span>
        <strong v-if="cell.row">{{ cell.row.net_pnl == null ? '不可用' : formatMoney(cell.row.net_pnl, compact ? 0 : 2) }}</strong>
        <small v-if="cell.row && !compact">{{ cell.row.campaign_count }} Campaign · {{ cell.row.fill_count }} fills</small>
        <i v-else-if="!cell.row">—</i>
      </button>
    </template>
  </div>
</template>
