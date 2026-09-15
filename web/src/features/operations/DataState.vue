<script setup lang="ts">
import { computed } from 'vue'
import { AlertTriangle, DatabaseZap, RefreshCw } from 'lucide-vue-next'

/**
 * 数据读取状态门面：加载中 / 读取失败 / 空数据 / 正常内容 四态。
 *
 * 合并了原先并行的回测查询面板（内联错误条）：
 * - `variant="page"`（默认）：`a-result` 大块错误页，用于运营控制台整页读取。
 * - `variant="inline"`：内联错误条，用于表格、图表等局部区块。
 *
 * 上游同时存在 `loading`（运营页面）与 `pending`（回测页面）两套命名，
 * 以及 string / Error / ApiError 三种错误负载，这里统一兼容。
 */
const props = withDefaults(
  defineProps<{
    /** 加载中（运营页面在用的命名）。 */
    loading?: boolean
    /** 加载中（回测页历史上的命名），与 `loading` 等价，任一为 true 即加载中。 */
    pending?: boolean
    /** 读取失败。接受 string / Error / ApiError 等任意负载。 */
    error?: unknown
    empty?: boolean
    /** 加载中文案；不传时按 variant 取默认值。 */
    loadingText?: string
    /** 空数据文案；不传时按 variant 取默认值。 */
    emptyText?: string
    variant?: 'page' | 'inline'
  }>(),
  { variant: 'page' },
)

defineEmits<{ retry: [] }>()

/** 提取可读的错误文案；无法提取时返回空串，由调用处回退到默认文案。 */
function readErrorMessage(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value.trim()
  if (value instanceof Error) return value.message.trim()
  if (typeof value === 'object') {
    const record = value as { message?: unknown; detail?: unknown; error?: unknown }
    for (const key of ['message', 'detail', 'error'] as const) {
      const candidate = record[key]
      if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
    }
    return ''
  }
  return String(value).trim()
}

/**
 * 注意：Boolean 类型的 prop 缺席时会被 Vue 归一成 `false` 而不是 `undefined`，
 * 所以这里不能用 `??` 做回退，否则 `pending` 永远不会生效。
 */
const isPending = computed(() => Boolean(props.loading || props.pending))
const hasError = computed(() => Boolean(props.error))
const resolvedLoadingText = computed(
  () => props.loadingText ?? (props.variant === 'inline' ? '正在加载' : '正在读取账本数据…'),
)
const resolvedEmptyText = computed(
  () => props.emptyText ?? (props.variant === 'inline' ? '暂无数据' : '当前筛选条件下没有数据'),
)
const resolvedErrorText = computed(() => {
  const message = readErrorMessage(props.error)
  if (message) return message
  return props.variant === 'inline' ? '请求失败' : '请稍后重试'
})
</script>

<template>
  <template v-if="variant === 'inline'">
    <div v-if="isPending" class="query-state">
      <a-spin size="small" />
      <span>{{ resolvedLoadingText }}</span>
    </div>
    <div v-else-if="hasError" class="query-state error-state">
      <AlertTriangle :size="20" />
      <span>{{ resolvedErrorText }}</span>
      <a-button size="small" @click="$emit('retry')">
        <template #icon><RefreshCw :size="14" /></template>
        重试
      </a-button>
    </div>
    <a-empty v-else-if="empty" :description="resolvedEmptyText" class="query-empty" />
    <slot v-else />
  </template>

  <template v-else>
    <div v-if="isPending" class="operation-state">
      <a-spin size="small" />
      <span>{{ resolvedLoadingText }}</span>
    </div>
    <a-result
      v-else-if="hasError"
      status="error"
      title="数据读取失败"
      :sub-title="resolvedErrorText"
      class="operation-result"
    >
      <template #icon><AlertTriangle :size="42" /></template>
      <template #extra><a-button @click="$emit('retry')">重新读取</a-button></template>
    </a-result>
    <a-empty v-else-if="empty" :description="resolvedEmptyText" class="operation-empty">
      <template #image><DatabaseZap :size="42" /></template>
    </a-empty>
    <slot v-else />
  </template>
</template>
