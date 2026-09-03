<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { BacktestEvent } from '@/api/types'
import { eventParameterRows } from './eventPresentation'

const props = withDefaults(
  defineProps<{
    event: BacktestEvent
    referenceData?: Record<string, unknown>
    pricePrecision?: number
  }>(),
  {
    pricePrecision: 2,
  },
)

const containerRef = ref<HTMLElement | null>(null)
const initialWidth = typeof window !== 'undefined' ? window.innerWidth || 1024 : 1024
const containerWidth = ref(initialWidth)

let resizeObserver: ResizeObserver | null = null

onMounted(() => {
  if (containerRef.value) {
    containerWidth.value = containerRef.value.clientWidth || window.innerWidth || 1024
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const width = entry.contentRect.width || (entry.target as HTMLElement).clientWidth
          if (width > 0) {
            containerWidth.value = width
          }
        }
      })
      resizeObserver.observe(containerRef.value)
    }
  }
})

onUnmounted(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
})

const rows = computed(() => eventParameterRows(props.event, props.referenceData, props.pricePrecision))

/**
 * 根据容器实际可用宽度和参数总项数，智能计算最佳分列数量。
 * 单表最小舒适宽度约为 420px，避免过窄导致换行严重；
 * 同时保证每列至少约 3 项，避免参数项被过度打散。
 */
const targetCols = computed(() => {
  const count = rows.value.length
  if (count <= 3) return 1
  // 每列至少保留 3 项
  const maxColsByItems = Math.max(1, Math.floor(count / 3))
  // 单列最小舒适宽 420px + 间距 10px
  const width = containerWidth.value || 1024
  const maxColsByWidth = Math.max(1, Math.floor((width + 10) / 420))
  return Math.max(1, Math.min(maxColsByItems, maxColsByWidth))
})

const splitColumns = computed(() => {
  const cols = targetCols.value
  const list = rows.value
  if (cols <= 1 || list.length <= 3) {
    return [list]
  }
  const result: Array<typeof list> = []
  const baseSize = Math.floor(list.length / cols)
  const remainder = list.length % cols
  let offset = 0
  for (let i = 0; i < cols; i++) {
    const size = baseSize + (i < remainder ? 1 : 0)
    result.push(list.slice(offset, offset + size))
    offset += size
  }
  return result
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
  <p v-if="description" class="event-description">
    {{ description }}
  </p>
  <div v-if="rows.length" ref="containerRef" class="event-tables-grid" :style="{ '--col-count': targetCols }">
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
            <th scope="col">门槛值</th>
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
            <td class="threshold-value">
              {{ row.threshold ?? row.reference }}
            </td>
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
  grid-template-columns: repeat(var(--col-count, 1), minmax(0, 1fr));
  gap: 10px;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  margin-top: 8px;
  box-sizing: border-box;
}

@media (max-width: 700px) {
  .event-tables-grid {
    grid-template-columns: 1fr !important;
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

.event-parameters thead th:nth-child(1) {
  width: 50%;
}
.event-parameters thead th:nth-child(2) {
  width: 30%;
}
.event-parameters thead th:nth-child(3) {
  width: 20%;
}

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

.event-parameters .threshold-value,
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
    font-size: var(--type-meta);
  }
  .event-parameters thead th:nth-child(1) {
    width: 37%;
  }
  .event-parameters thead th:nth-child(2) {
    width: 31%;
  }
  .event-parameters thead th:nth-child(3) {
    width: 32%;
  }
  .major-mark {
    margin-left: 2px;
    font-size: var(--type-meta);
  }
  .event-parameters tbody th small {
    font-size: var(--type-meta);
  }
}
</style>
