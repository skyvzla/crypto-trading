<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => String(route.meta.title ?? route.name ?? ''))
const needsApi = computed(() =>
  route.meta.needsApi ? String(route.meta.needsApi) : null
)
</script>

<template>
  <a-card :title="title">
    <a-empty description="视图待实现">
      <template #extra>
        <a-space direction="vertical" align="center" :size="8">
          <a-tag color="blue">骨架阶段</a-tag>
          <a-tag v-if="needsApi" color="orange">
            依赖后端：{{ needsApi }}
          </a-tag>
        </a-space>
      </template>
    </a-empty>
  </a-card>
</template>
