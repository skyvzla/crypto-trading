<script setup lang="ts">
import { Pencil, Plus, Trash2 } from 'lucide-vue-next'
import type { NotificationPolicy } from '@/api/types'

defineProps<{ policies: NotificationPolicy[]; groupNames: (ids: string[]) => string; severityLabel: (value: string) => string; statusBadge: (value: string) => 'success' | 'processing' | 'warning' | 'error' | 'default' }>()
const emit = defineEmits<{
  new: []
  edit: [item: NotificationPolicy]
  toggle: [item: NotificationPolicy, enabled: boolean]
  delete: [item: NotificationPolicy]
}>()
</script>

<template>
  <section class="view-panel" aria-labelledby="policies-heading">
    <div class="section-heading"><div><span class="section-kicker">ROUTING LOGIC</span><h2 id="policies-heading">路由策略</h2></div><a-button type="primary" @click="emit('new')"><template #icon><Plus :size="15" /></template>新建策略</a-button></div>
    <p class="section-description">精确事件模式优先，其次按优先级选择；一个策略可通知一个或多个职责组，也可以显式抑制。</p>
    <div class="data-table-wrap surface-panel">
      <a-table :data-source="policies" :pagination="false" row-key="id" size="small">
        <a-table-column key="name" title="策略" :width="190"><template #default="{ record }"><div class="primary-cell"><strong>{{ record.name }}</strong><small>v{{ record.version }} · {{ record.suppress ? '显式抑制' : '正常路由' }}</small></div></template></a-table-column>
        <a-table-column key="pattern" title="事件模式"><template #default="{ record }"><code class="pattern-code">{{ record.event_pattern }}</code></template></a-table-column>
        <a-table-column key="severity" title="级别" :width="92"><template #default="{ record }"><a-badge :status="statusBadge(record.severity)" :text="severityLabel(record.severity)" /></template></a-table-column>
        <a-table-column key="priority" title="优先级" :width="76"><template #default="{ record }"><span class="mono-value">{{ record.priority }}</span></template></a-table-column>
        <a-table-column key="groups" title="职责组"><template #default="{ record }"><span class="wrap-value">{{ record.suppress ? '—' : groupNames(record.group_ids) }}</span></template></a-table-column>
        <a-table-column key="enabled" title="状态" :width="100"><template #default="{ record }"><a-switch :checked="record.enabled" size="small" @change="(checked: boolean) => emit('toggle', record, checked)" /></template></a-table-column>
        <a-table-column key="actions" title="操作" :width="116"><template #default="{ record }"><a-button type="text" aria-label="编辑路由策略" @click="emit('edit', record)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此路由策略？" ok-text="删除" cancel-text="取消" @confirm="emit('delete', record)"><a-button type="text" danger aria-label="删除路由策略"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></template></a-table-column>
      </a-table>
      <div v-if="!policies.length" class="table-empty"><a-empty description="暂无路由策略" /></div>
    </div>
  </section>
</template>
