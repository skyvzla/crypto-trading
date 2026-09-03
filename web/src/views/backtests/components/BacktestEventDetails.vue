<script setup lang="ts">
import { computed } from 'vue'
import type { BacktestEvent } from '@/api/types'
import { eventParameterGroups } from './eventPresentation'

const props = defineProps<{ event: BacktestEvent; referenceData?: Record<string, unknown> }>()
const groups = computed(() => eventParameterGroups(props.event, props.referenceData))
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
  <div v-if="groups.length" class="event-groups" role="region" aria-label="事件分类指标与参数">
    <div v-for="group in groups" :key="group.id" class="event-group-card" :data-group="group.id">
      <div class="event-group-header">
        <span class="group-title">{{ group.title }}</span>
        <span class="group-count">{{ group.rows.length }} 项</span>
      </div>
      <table class="event-parameters">
        <thead>
          <tr>
            <th scope="col">参数 / 指标</th>
            <th scope="col">参数值</th>
            <th scope="col">参考值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in group.rows" :key="row.key" :class="{ 'is-major': row.major }">
            <th scope="row">
              <span>{{ row.label }}</span>
              <b v-if="row.major" class="major-mark">主要</b>
              <small>{{ row.key }}</small>
            </th>
            <td>{{ row.value }}</td>
            <td class="reference-value">{{ row.reference }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped lang="scss">
.event-description { margin: 5px 0 0; color: var(--text); font-size: var(--type-secondary); }
.event-groups {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
  margin-top: 8px;
}
.event-group-card {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  overflow: hidden;
}
.event-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-hover);
  font-size: var(--type-meta);
}
.group-title { font-weight: 600; color: var(--text); }
.group-count { color: var(--muted); font-size: var(--type-meta); }
.event-parameters { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: var(--type-meta); }
.event-parameters th, .event-parameters td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
.event-parameters thead th {
  color: var(--muted);
  background: color-mix(in srgb, var(--surface-hover) 60%, var(--surface));
  font-weight: 500;
}
.event-parameters thead th:nth-child(1) { width: 44%; }
.event-parameters thead th:nth-child(2) { width: 28%; }
.event-parameters thead th:nth-child(3) { width: 28%; }
.event-parameters tbody tr:last-child > * { border-bottom: 0; }
.event-parameters tbody th { color: var(--text); font-weight: 500; }
.major-mark { display: inline-block; margin-left: 5px; color: var(--color-info); font-size: var(--type-meta); font-weight: 600; }
.event-parameters tbody th small { display: block; margin-top: 2px; color: var(--muted); font: var(--type-meta)/1.25 var(--font-family-mono); }
.event-parameters td { color: var(--text); font-family: var(--font-family-mono); }
.event-parameters .reference-value { color: var(--muted); }
.event-parameters tr.is-major { background: color-mix(in srgb, var(--color-info) 7%, transparent); }
.event-parameters tr.is-major th > span { font-weight: 650; }
</style>
