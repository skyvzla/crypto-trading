<script setup lang="ts">
import { Pencil, Plus, Trash2 } from 'lucide-vue-next'
import type { NotificationEndpoint, NotificationGroup } from '@/api/types'
import { nameList } from './presentation'
import NotificationSectionHeader from './NotificationSectionHeader.vue'

const props = defineProps<{ groups: NotificationGroup[]; endpointById: Map<string, NotificationEndpoint> }>()
const emit = defineEmits<{
  new: []
  edit: [item: NotificationGroup]
  toggle: [item: NotificationGroup, enabled: boolean]
  delete: [item: NotificationGroup]
}>()
</script>

<template>
  <section class="view-panel" aria-labelledby="groups-heading">
    <NotificationSectionHeader id="groups-heading" kicker="RESPONSIBILITY MAP" title="职责组" description="把端点编成稳定的职责边界。策略只引用职责组，替换 Bot、群组或 URL 时无需修改策略。">
      <template #actions><a-button type="primary" @click="emit('new')"><template #icon><Plus :size="15" /></template>新建职责组</a-button></template>
    </NotificationSectionHeader>
    <div class="data-table-wrap data-card">
      <a-table :data-source="groups" :pagination="false" :scroll="{ x: 700 }" row-key="id" size="small">
        <a-table-column key="name" title="职责组" :width="220"><template #default="{ record }"><div class="primary-cell"><strong>{{ record.name }}</strong><small>{{ record.description || '未填写说明' }}</small></div></template></a-table-column>
        <a-table-column key="endpoints" title="成员端点"><template #default="{ record }"><span class="wrap-value">{{ nameList(record.endpoint_ids, props.endpointById, '未配置端点') }}</span></template></a-table-column>
        <a-table-column key="version" title="版本" :width="80"><template #default="{ record }"><span class="mono-value">v{{ record.version }}</span></template></a-table-column>
        <a-table-column key="enabled" title="状态" :width="100"><template #default="{ record }"><a-switch :checked="record.enabled" size="small" @change="(checked: boolean) => emit('toggle', record, checked)" /></template></a-table-column>
        <a-table-column key="actions" title="操作" :width="116"><template #default="{ record }"><a-button type="text" aria-label="编辑职责组" @click="emit('edit', record)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此职责组？" ok-text="删除" cancel-text="取消" @confirm="emit('delete', record)"><a-button type="text" danger aria-label="删除职责组"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></template></a-table-column>
      </a-table>
      <div v-if="!groups.length" class="table-empty"><a-empty description="暂无职责组" /></div>
    </div>
  </section>
</template>

<style scoped lang="scss">
.view-panel { min-width: 0; }
.data-card-heading > div { display: flex; align-items: center; gap: 7px; min-width: 0; }
.data-card-heading h3 { margin: 0; font-size: var(--type-primary); letter-spacing: 0; }
.panel-icon { color: var(--color-primary); }
.data-table-wrap { overflow: hidden; }
.wrap-value { display: block; max-width: 420px; overflow-wrap: anywhere; color: var(--text); font-size: var(--type-secondary); line-height: 1.45; }
.table-empty { padding: 30px 0; }
.mono-value { color: var(--muted); font: var(--type-meta) var(--font-family-mono); }
</style>
