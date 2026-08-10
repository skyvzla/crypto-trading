<script setup lang="ts">
import { ArrowLeft } from 'lucide-vue-next'
import { NButton, NBreadcrumb, NBreadcrumbItem, NIcon, NTooltip } from 'naive-ui'
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
        <NBreadcrumb>
          <NBreadcrumbItem v-for="crumb in crumbs" :key="crumb.label">
            <RouterLink v-if="crumb.to" :to="crumb.to">{{ crumb.label }}</RouterLink>
            <span v-else>{{ crumb.label }}</span>
          </NBreadcrumbItem>
        </NBreadcrumb>
        <span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span>
        <h2>{{ title }}</h2>
      </div>
      <div class="page-actions">
        <slot name="actions" />
        <NTooltip v-if="backTo" trigger="hover">
          <template #trigger>
            <NButton quaternary circle aria-label="返回" @click="router.push(backTo!)">
              <template #icon><NIcon :component="ArrowLeft" /></template>
            </NButton>
          </template>
          返回
        </NTooltip>
      </div>
    </div>
    <slot />
  </section>
</template>
