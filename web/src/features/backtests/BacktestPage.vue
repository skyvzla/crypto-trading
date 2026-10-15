<script setup lang="ts">
import { computed } from 'vue'
import { ArrowLeft } from 'lucide-vue-next'
import { useRouter, type RouteLocationRaw } from 'vue-router'

const props = defineProps<{
  title: string
  eyebrow?: string
  backTo?: RouteLocationRaw
  crumbs: Array<{ label: string; to?: RouteLocationRaw }>
}>()

const router = useRouter()

/** 过滤掉纯装饰性的 RESEARCH ARCHIVE 口号，只保留真实的实体 ID（如 researchId / tradeId）。 */
const entityBadge = computed(() => {
  if (!props.eyebrow || props.eyebrow === 'RESEARCH ARCHIVE') return ''
  return props.eyebrow
})
</script>

<template>
  <section class="backtest-page">
    <div class="page-heading">
      <div class="heading-copy">
        <div class="heading-title-row">
          <a-tooltip v-if="backTo" title="返回">
            <a-button type="text" shape="circle" aria-label="返回" @click="router.push(backTo!)">
              <ArrowLeft :size="16" />
            </a-button>
          </a-tooltip>
          <h2>{{ title }}</h2>
          <span v-if="entityBadge" class="eyebrow-inline">{{ entityBadge }}</span>
        </div>
      </div>
      <div v-if="$slots.actions" class="page-actions">
        <slot name="actions" />
      </div>
    </div>
    <slot />
  </section>
</template>
