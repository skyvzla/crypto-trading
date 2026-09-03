<script setup lang="ts">
import { computed } from 'vue'
import dayjs from 'dayjs'
import { asNumber, formatMoney, pnlClass } from '@/shared/format'

interface DailyPnlRow {
  date: string
  net_pnl: string | number | null
  campaign_count: number
  fill_count: number
}

const props = withDefaults(
  defineProps<{
    year: number
    month: number
    rows: DailyPnlRow[]
    compact?: boolean
  }>(),
  { compact: false },
)

const emit = defineEmits<{ day: [date: string] }>()

const weekdays = ['一', '二', '三', '四', '五', '六', '日']

/**
 * 月历格子。前导空格按周一开头计算。
 *
 * 这里只做「年月 → 该月每一天」的纯日期推算，与时区无关：
 * 传入的 year/month 已经是账本时区下的自然月。
 */
const cells = computed(() => {
  const firstDay = dayjs(`${props.year}-${String(props.month).padStart(2, '0')}-01`)
  const leading = (firstDay.day() + 6) % 7
  const byDate = new Map(props.rows.map((row) => [row.date, row]))
  const blanks: Array<{ date: string; day: number; row?: DailyPnlRow } | null> = Array.from(
    { length: leading },
    () => null,
  )
  const days = Array.from({ length: firstDay.daysInMonth() }, (_, index) => {
    const date = firstDay.add(index, 'day').format('YYYY-MM-DD')
    return { date, day: index + 1, row: byDate.get(date) }
  })
  return [...blanks, ...days]
})

/** 亏盈色块深浅，按当月最大绝对值归一化。 */
const maxAbsolutePnl = computed(() =>
  Math.max(1, ...props.rows.filter((item) => item.net_pnl != null).map((item) => Math.abs(asNumber(item.net_pnl)))),
)

function intensity(row?: DailyPnlRow): number {
  if (!row || row.net_pnl == null) return 0
  return Math.max(0.12, Math.min(0.58, (Math.abs(asNumber(row.net_pnl)) / maxAbsolutePnl.value) * 0.58))
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
        <strong v-if="cell.row">{{
          cell.row.net_pnl == null ? '不可用' : formatMoney(cell.row.net_pnl, compact ? 0 : 2)
        }}</strong>
        <small v-if="cell.row && !compact"
          >{{ cell.row.campaign_count }} Campaign · {{ cell.row.fill_count }} fills</small
        >
        <i v-else-if="!cell.row">—</i>
      </button>
    </template>
  </div>
</template>
