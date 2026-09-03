<script setup lang="ts">
import { Pencil, Plus, Trash2 } from 'lucide-vue-next'
import type { NotificationGroup, NotificationPolicy } from '@/api/types'
import { nameList, severityLabel, statusBadge } from './presentation'
import NotificationSectionHeader from './NotificationSectionHeader.vue'

const props = defineProps<{ policies: NotificationPolicy[]; groupById: Map<string, NotificationGroup> }>()
const emit = defineEmits<{
  new: []
  edit: [item: NotificationPolicy]
  toggle: [item: NotificationPolicy, enabled: boolean]
  delete: [item: NotificationPolicy]
}>()
</script>

<template>
  <section class="view-panel" aria-labelledby="policies-heading">
    <NotificationSectionHeader id="policies-heading" kicker="ROUTING LOGIC" title="路由策略" description="精确事件模式优先，其次按优先级选择；一个策略可通知一个或多个职责组，也可以显式抑制。">
      <template #actions><a-button type="primary" @click="emit('new')"><template #icon><Plus :size="15" /></template>新建策略</a-button></template>
    </NotificationSectionHeader>
    <div class="data-table-wrap data-card">
      <a-table :data-source="policies" :pagination="false" :scroll="{ x: 700 }" row-key="id" size="small">
        <a-table-column key="name" title="策略" :width="190"><template #default="{ record }"><div class="primary-cell"><strong>{{ record.name }}</strong><small>v{{ record.version }} · {{ record.suppress ? '显式抑制' : '正常路由' }}</small></div></template></a-table-column>
        <a-table-column key="pattern" title="事件模式"><template #default="{ record }"><code class="pattern-code">{{ record.event_pattern }}</code></template></a-table-column>
        <a-table-column key="severity" title="级别" :width="92"><template #default="{ record }"><a-badge :status="statusBadge(record.severity)" :text="severityLabel(record.severity)" /></template></a-table-column>
        <a-table-column key="priority" title="优先级" :width="76"><template #default="{ record }"><span class="mono-value">{{ record.priority }}</span></template></a-table-column>
        <a-table-column key="groups" title="职责组"><template #default="{ record }"><span class="wrap-value">{{ record.suppress ? '—' : nameList(record.group_ids, props.groupById, '未选择职责组') }}</span></template></a-table-column>
        <a-table-column key="enabled" title="状态" :width="100"><template #default="{ record }"><a-switch :checked="record.enabled" size="small" @change="(checked: boolean) => emit('toggle', record, checked)" /></template></a-table-column>
        <a-table-column key="actions" title="操作" :width="116"><template #default="{ record }"><a-button type="text" aria-label="编辑路由策略" @click="emit('edit', record)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此路由策略？" ok-text="删除" cancel-text="取消" @confirm="emit('delete', record)"><a-button type="text" danger aria-label="删除路由策略"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></template></a-table-column>
      </a-table>
      <div v-if="!policies.length" class="table-empty"><a-empty description="暂无路由策略" /></div>
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
.pattern-code { display: inline-block; max-width: 260px; padding: 3px 6px; border: 1px solid var(--line); border-radius: 4px; color: var(--text); background: var(--surface-hover); font: var(--type-meta) var(--font-family-mono); overflow-wrap: anywhere; }
</style>
