<script setup lang="ts">
import { AlertTriangle, RefreshCw } from 'lucide-vue-next'
import { NButton, NEmpty, NIcon, NSpin } from 'naive-ui'

defineProps<{
  pending?: boolean
  error?: Error | null
  empty?: boolean
  emptyText?: string
}>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="pending" class="query-state"><NSpin size="small" /><span>正在加载</span></div>
  <div v-else-if="error" class="query-state error-state">
    <NIcon :component="AlertTriangle" size="20" />
    <span>{{ error.message || '请求失败' }}</span>
    <NButton size="small" secondary @click="$emit('retry')">
      <template #icon><NIcon :component="RefreshCw" /></template>
      重试
    </NButton>
  </div>
  <NEmpty v-else-if="empty" :description="emptyText || '暂无数据'" class="query-empty" />
  <slot v-else />
</template>
