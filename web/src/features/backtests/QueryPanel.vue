<script setup lang="ts">
import { AlertTriangle, RefreshCw } from 'lucide-vue-next'

defineProps<{
  pending?: boolean
  error?: Error | null
  empty?: boolean
  emptyText?: string
}>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="pending" class="query-state"><a-spin size="small" /><span>正在加载</span></div>
  <div v-else-if="error" class="query-state error-state">
    <AlertTriangle :size="20" />
    <span>{{ error.message || '请求失败' }}</span>
    <a-button size="small" @click="$emit('retry')">
      <template #icon><RefreshCw :size="14" /></template>
      重试
    </a-button>
  </div>
  <a-empty v-else-if="empty" :description="emptyText || '暂无数据'" class="query-empty" />
  <slot v-else />
</template>
