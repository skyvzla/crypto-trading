<script setup lang="ts">
import { AlertTriangle, DatabaseZap } from 'lucide-vue-next'

defineProps<{
  loading?: boolean
  error?: string | null
  empty?: boolean
  emptyText?: string
}>()

defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="loading" class="operation-state">
    <a-spin size="small" />
    <span>正在读取账本数据…</span>
  </div>
  <a-result v-else-if="error" status="error" title="数据读取失败" :sub-title="error" class="operation-result">
    <template #icon><AlertTriangle :size="42" /></template>
    <template #extra><a-button @click="$emit('retry')">重新读取</a-button></template>
  </a-result>
  <a-empty v-else-if="empty" :description="emptyText || '当前筛选条件下没有数据'" class="operation-empty">
    <template #image><DatabaseZap :size="42" /></template>
  </a-empty>
  <slot v-else />
</template>
