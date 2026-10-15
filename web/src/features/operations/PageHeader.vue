<script setup lang="ts">
import { RefreshCw } from 'lucide-vue-next'

defineProps<{
  eyebrow?: string
  title: string
  description: string
  loading?: boolean
  refreshedAt?: string | null
}>()

defineEmits<{ refresh: [] }>()
</script>

<template>
  <header class="operations-heading">
    <div class="operations-heading-copy">
      <h1>{{ title }}</h1>
      <p>{{ description }}</p>
    </div>
    <div class="operations-heading-actions">
      <slot name="actions" />
      <a-tooltip :title="refreshedAt ? `刷新数据（最近同步 ${refreshedAt}）` : '刷新数据'">
        <a-button :loading="loading" aria-label="刷新数据" @click="$emit('refresh')">
          <template #icon><RefreshCw :size="14" /></template>
          刷新
        </a-button>
      </a-tooltip>
    </div>
  </header>
</template>
