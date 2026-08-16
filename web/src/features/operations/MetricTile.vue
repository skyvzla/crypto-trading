<script setup lang="ts">
defineProps<{
  label: string
  value: string
  hint?: string
  tone?: 'positive' | 'negative' | 'warning' | 'neutral'
}>()
</script>

<template>
  <article class="metric-tile" :class="`metric-${tone || 'neutral'}`">
    <span>{{ label }}</span>
    <strong>{{ value }}</strong>
    <small v-if="hint">{{ hint }}</small>
  </article>
</template>
