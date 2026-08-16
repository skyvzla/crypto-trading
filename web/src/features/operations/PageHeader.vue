<script setup lang="ts">
import { RefreshCw } from 'lucide-vue-next'

defineProps<{
  eyebrow: string
  title: string
  description: string
  loading?: boolean
  refreshedAt?: string | null
}>()

defineEmits<{ refresh: [] }>()
</script>

<template>
  <header class="operations-heading">
    <div>
      <span class="eyebrow">{{ eyebrow }}</span>
      <h1>{{ title }}</h1>
      <p>{{ description }}</p>
    </div>
    <div class="operations-heading-actions">
      <span v-if="refreshedAt" class="refresh-stamp">DATA // {{ refreshedAt }}</span>
      <slot name="actions" />
      <a-button :loading="loading" aria-label="刷新数据" @click="$emit('refresh')">
        <template #icon><RefreshCw :size="14" /></template>
        刷新
      </a-button>
    </div>
  </header>
</template>
