<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { NCard, NEmpty, NTag, NSpace } from 'naive-ui'

const route = useRoute()
const title = computed(() => String(route.meta.title ?? route.name ?? ''))
const needsApi = computed(() =>
  route.meta.needsApi ? String(route.meta.needsApi) : null
)
</script>

<template>
  <NCard :title="title" :bordered="true">
    <NEmpty description="视图待实现">
      <template #extra>
        <NSpace vertical align="center" :size="8">
          <NTag :bordered="false" size="small" type="info">骨架阶段</NTag>
          <NTag v-if="needsApi" :bordered="false" size="small" type="warning">
            依赖后端：{{ needsApi }}
          </NTag>
        </NSpace>
      </template>
    </NEmpty>
  </NCard>
</template>
