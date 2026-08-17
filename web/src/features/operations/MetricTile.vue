<script setup lang="ts">
import { RouterLink, type RouteLocationRaw } from 'vue-router'

defineProps<{
  label: string
  value: string
  hint?: string
  tone?: 'positive' | 'negative' | 'warning' | 'neutral'
  to?: RouteLocationRaw
}>()
</script>

<template>
  <RouterLink v-if="to" :to="to" class="metric-link">
    <article class="metric-tile" :class="`metric-${tone || 'neutral'}`">
      <span>{{ label }}</span><strong>{{ value }}</strong><small v-if="hint">{{ hint }}</small>
    </article>
  </RouterLink>
  <article v-else class="metric-tile" :class="`metric-${tone || 'neutral'}`">
    <span>{{ label }}</span><strong>{{ value }}</strong><small v-if="hint">{{ hint }}</small>
  </article>
</template>

<style scoped>
.metric-link { display:block; color:inherit; text-decoration:none; }
.metric-link .metric-tile { height:100%; transition:border-color .15s ease, transform .15s ease; }
.metric-link:hover .metric-tile,.metric-link:focus-visible .metric-tile { border-color:var(--color-gold); transform:translateY(-1px); }
</style>
