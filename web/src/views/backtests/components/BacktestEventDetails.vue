<script setup lang="ts">
import { computed } from 'vue'
import type { BacktestEvent } from '@/api/types'
import { eventParameterRows } from './eventPresentation'

const props = withDefaults(defineProps<{
  event: BacktestEvent
  referenceData?: Record<string, unknown>
  pricePrecision?: number
}>(), {
  pricePrecision: 2
})

const rows = computed(() => eventParameterRows(props.event, props.referenceData, props.pricePrecision))

const splitColumns = computed(() => {
  if (rows.value.length <= 4) {
    return [rows.value]
  }
  const mid = Math.ceil(rows.value.length / 2)
  return [rows.value.slice(0, mid), rows.value.slice(mid)]
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
  <div v-if="rows.length" class="event-tables-grid" :class="{ 'is-split': splitColumns.length > 1 }">
    <div
      v-for="(colRows, idx) in splitColumns"
      :key="idx"
      class="event-parameters-wrap"
      tabindex="0"
      aria-label="事件参数表"
    >
      <table class="event-parameters">
        <thead>
          <tr>
            <th scope="col">参数 / 指标</th>
            <th scope="col">参数值</th>
            <th scope="col">参考值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in colRows" :key="row.key" :class="{ 'is-major': row.major }">
            <th scope="row">
              <span>{{ row.label }}</span>
              <b v-if="row.major" class="major-mark">主要</b>
              <small>{{ row.key }}</small>
            </th>
            <td class="param-value">{{ row.value }}</td>
            <td class="reference-value">{{ row.reference }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped lang="scss">
.event-description {
  margin: 5px 0 0;
  color: var(--text);
  font-size: var(--type-secondary);
}

.event-tables-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  margin-top: 8px;
  box-sizing: border-box;
}

@media (min-width: 1400px) {
  .event-tables-grid.is-split {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.event-parameters-wrap {
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
}

.event-parameters {
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: var(--type-meta);
}

.event-parameters th,
.event-parameters td {
  box-sizing: border-box;
  padding: 6px 7px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
  word-break: break-word;
  min-width: 0;
}

.event-parameters thead th {
  color: var(--muted);
  background: color-mix(in srgb, var(--surface-hover) 60%, var(--surface));
  font-weight: 500;
}

.event-parameters thead th:nth-child(1) { width: 38%; }
.event-parameters thead th:nth-child(2) { width: 30%; }
.event-parameters thead th:nth-child(3) { width: 32%; }

.event-parameters tbody tr:last-child > * {
  border-bottom: 0;
}

.event-parameters tbody th {
  color: var(--text);
  font-weight: 500;
}

.major-mark {
  display: inline-block;
  margin-left: 5px;
  color: var(--color-info);
  font-size: var(--type-meta);
  font-weight: 600;
}

.event-parameters tbody th small {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font: var(--type-meta)/1.25 var(--font-family-mono);
}

.event-parameters td {
  color: var(--text);
  font-family: var(--font-family-mono);
}

.event-parameters .reference-value {
  color: var(--muted);
}

.event-parameters tr.is-major {
  background: color-mix(in srgb, var(--color-info) 7%, transparent);
}

.event-parameters tr.is-major th > span {
  font-weight: 650;
}

@media (max-width: 600px) {
  .event-parameters th,
  .event-parameters td {
    padding: 5px 4px;
    font-size: 11px;
  }
  .event-parameters thead th:nth-child(1) { width: 37%; }
  .event-parameters thead th:nth-child(2) { width: 31%; }
  .event-parameters thead th:nth-child(3) { width: 32%; }
  .major-mark {
    margin-left: 2px;
    font-size: 10px;
  }
  .event-parameters tbody th small {
    font-size: 10px;
  }
}
</style>
