<script setup lang="ts">
import { ArrowLeft } from 'lucide-vue-next'
import { useRouter, type RouteLocationRaw } from 'vue-router'

defineProps<{
  title: string
  eyebrow?: string
  backTo?: RouteLocationRaw
  crumbs: Array<{ label: string; to?: RouteLocationRaw }>
}>()

const router = useRouter()
</script>

<template>
  <section class="backtest-page">
    <div class="page-heading">
      <div class="heading-copy">
        <a-breadcrumb>
          <a-breadcrumb-item v-for="crumb in crumbs" :key="crumb.label">
            <RouterLink v-if="crumb.to" :to="crumb.to">{{ crumb.label }}</RouterLink>
            <span v-else>{{ crumb.label }}</span>
          </a-breadcrumb-item>
        </a-breadcrumb>
        <span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span>
        <h2>{{ title }}</h2>
      </div>
      <div class="page-actions">
        <slot name="actions" />
        <a-tooltip v-if="backTo" title="返回">
          <template #trigger>
            <a-button type="text" shape="circle" aria-label="返回" @click="router.push(backTo!)"><ArrowLeft :size="16" /></a-button>
          </template>
        </a-tooltip>
      </div>
    </div>
    <slot />
  </section>
</template>
