<script setup lang="ts">
import { computed } from 'vue'
import { NDescriptions, NDescriptionsItem, NEmpty } from 'naive-ui'
import type { JsonObject, StrategyField, StrategyGroup } from '@/api/types'
import { displayValue } from './format'

const props = defineProps<{
  value?: JsonObject
  groups?: StrategyGroup[]
  fields?: StrategyField[]
}>()

const fieldGroups = computed<StrategyGroup[]>(() => {
  if (props.groups?.length) return props.groups
  if (props.fields?.length) return [{ key: 'strategy', label: '策略参数', fields: props.fields }]
  const rawFields: StrategyField[] = Object.keys(props.value ?? {}).map((key) => ({ key, label: key }))
  return rawFields.length ? [{ key: 'raw', label: '原始参数', fields: rawFields }] : []
})
</script>

<template>
  <NEmpty v-if="!fieldGroups.length" description="无扩展参数" />
  <div v-else class="json-groups">
    <section v-for="group in fieldGroups" :key="group.key" class="detail-section">
      <h3>{{ group.label || group.key }}</h3>
      <NDescriptions :column="3" label-placement="top" bordered responsive="screen">
        <NDescriptionsItem
          v-for="field in group.fields.filter((item) => item.visible !== false)"
          :key="field.key"
          :label="field.label || field.key"
        >
          <span class="mono-value">{{ displayValue(value?.[field.key], field.format || field.type) }}</span>
        </NDescriptionsItem>
      </NDescriptions>
    </section>
  </div>
</template>
