<script setup lang="ts">
import { computed } from 'vue'
import type { BacktestEvent } from '@/api/types'
import { eventParameterRows } from './eventPresentation'

const props = defineProps<{ event: BacktestEvent; referenceData?: Record<string, unknown> }>()
const rows = computed(() => eventParameterRows(props.event, props.referenceData))
const rowPairs = computed(() => {
  const pairs: Array<[typeof rows.value[number], typeof rows.value[number] | null]> = []
  for (let index = 0; index < rows.value.length; index += 2) {
    pairs.push([rows.value[index], rows.value[index + 1] || null])
  }
  return pairs
})
const description = computed(() => {
  if (!props.event.description) return null
  const data = props.event.data || {}
  return [data.description, data.reason, data.message].some((value) => value === props.event.description)
    ? null
    : props.event.description
})
</script>

<template>
  <p v-if="description" class="event-description">{{ description }}</p>
  <div v-if="rows.length" class="event-parameters-wrap" tabindex="0" aria-label="事件参数表，可横向滚动">
    <table class="event-parameters">
      <thead><tr><th>参数 / 指标</th><th>参数值</th><th>参考值</th><th>参数 / 指标</th><th>参数值</th><th>参考值</th></tr></thead>
      <tbody>
        <tr v-for="([left, right]) in rowPairs" :key="left.key" :class="{ 'is-major': left.major || right?.major }">
          <th scope="row"><span>{{ left.label }}</span><b v-if="left.major" class="major-mark">主要</b><small>{{ left.key }}</small></th>
          <td>{{ left.value }}</td>
          <td class="reference-value">{{ left.reference }}</td>
          <template v-if="right">
            <th scope="row"><span>{{ right.label }}</span><b v-if="right.major" class="major-mark">主要</b><small>{{ right.key }}</small></th>
            <td>{{ right.value }}</td>
            <td class="reference-value">{{ right.reference }}</td>
          </template>
          <template v-else>
            <td colspan="3" class="empty-pair" aria-hidden="true"></td>
          </template>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped lang="scss">
.event-description { margin:5px 0 0; color:var(--text); font-size:var(--type-secondary); }
.event-parameters-wrap { margin-top:7px; overflow-x:auto; border:1px solid var(--line); border-radius:5px; }
.event-parameters { width:100%; min-width:820px; border-collapse:collapse; table-layout:fixed; font-size:var(--type-meta); }
.event-parameters th,.event-parameters td { padding:7px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; overflow-wrap:anywhere; }
.event-parameters thead th { color:var(--muted); background:var(--surface-hover); font-weight:500; }
.event-parameters thead th:nth-child(3n + 1) { width:22%; }
.event-parameters thead th:nth-child(3n + 2) { width:14%; }
.event-parameters thead th:nth-child(3n) { width:14%; }
.event-parameters thead th:nth-child(4) { border-left:1px solid var(--line); }
.event-parameters tbody td:nth-child(4), .event-parameters tbody td.empty-pair { border-left:1px solid var(--line); }
.event-parameters tbody tr:last-child > * { border-bottom:0; }
.event-parameters tbody th { color:var(--text); font-weight:500; }
.major-mark { display:inline-block; margin-left:5px; color:var(--color-info); font-size:var(--type-meta); font-weight:600; }
.event-parameters tbody th small { display:block; margin-top:2px; color:var(--muted); font:var(--type-meta)/1.25 var(--font-family-mono); }
.event-parameters td { color:var(--text); font-family:var(--font-family-mono); }
.event-parameters .reference-value { color:var(--muted); }
.event-parameters .empty-pair { background:var(--surface); }
.event-parameters tr.is-major { background:color-mix(in srgb, var(--color-info) 7%, transparent); }
.event-parameters tr.is-major th > span { font-weight:650; }
</style>
