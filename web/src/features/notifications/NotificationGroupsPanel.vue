<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, Pencil, Plus, Trash2 } from 'lucide-vue-next'
import type { NotificationEndpoint, NotificationGroup } from '@/api/types'
import NotificationSectionHeader from './NotificationSectionHeader.vue'

const props = defineProps<{
  groups: NotificationGroup[]
  endpoints: NotificationEndpoint[]
  endpointById: Map<string, NotificationEndpoint>
  connectorById: Map<string, { name: string; type: string }>
}>()
const emit = defineEmits<{
  new: []
  'new-endpoint': [connectorId?: string]
  'new-connector': []
  edit: [item: NotificationGroup]
  toggle: [item: NotificationGroup, enabled: boolean]
  delete: [item: NotificationGroup]
}>()

const canCreateGroup = computed(() => props.endpoints.length > 0)
const groupTableLocale = computed(() => ({
  emptyText: props.endpoints.length
    ? '暂无职责组'
    : props.connectorById.size
      ? '添加端点后即可创建职责组'
      : '创建连接器后即可添加端点',
}))
</script>

<template>
  <section class="view-panel" aria-labelledby="groups-heading">
    <NotificationSectionHeader
      id="groups-heading"
      kicker="RESPONSIBILITY MAP"
      title="职责组"
      description="把端点编成稳定的职责边界。策略只引用职责组，替换 Bot、群组或 URL 时无需修改策略。"
    >
      <template #actions>
        <a-button v-if="canCreateGroup" type="primary" @click="emit('new')">
          <template #icon><Plus :size="15" /></template>
          新建职责组
        </a-button>
        <a-button v-else-if="connectorById.size" type="primary" @click="emit('new-endpoint')">
          <template #icon><Plus :size="15" /></template>
          先添加端点
        </a-button>
        <a-button v-else type="primary" @click="emit('new-connector')">
          <template #icon><Plus :size="15" /></template>
          创建连接器
        </a-button>
      </template>
    </NotificationSectionHeader>
    <div class="dependency-strip" aria-label="通知配置依赖">
      <span class="dependency-step">
        <strong>{{ connectorById.size }}</strong>
        <small>连接器</small>
      </span>
      <ArrowRight :size="14" />
      <span class="dependency-step">
        <strong>{{ endpoints.length }}</strong>
        <small>端点</small>
      </span>
      <ArrowRight :size="14" />
      <span class="dependency-step">
        <strong>{{ groups.length }}</strong>
        <small>职责组</small>
      </span>
    </div>
    <a-alert v-if="!endpoints.length" class="dependency-alert" type="info" show-icon>
      <template #message>职责组需要先绑定端点</template>
      <template #description>
        <span v-if="connectorById.size">当前已有连接器，请先添加 Chat、Topic 或 Webhook 端点。</span>
        <span v-else>请先创建发送连接器，再添加端点，最后将端点编入职责组。</span>
      </template>
    </a-alert>
    <div class="data-table-wrap data-card">
      <a-table
        :data-source="groups"
        :pagination="false"
        :scroll="{ x: 700 }"
        row-key="id"
        size="small"
        :locale="groupTableLocale"
      >
        <a-table-column key="name" title="职责组" :width="220"
          ><template #default="{ record }"
            ><div class="primary-cell">
              <strong>{{ record.name }}</strong
              ><small>{{ record.description || '未填写说明' }}</small>
            </div></template
          ></a-table-column
        >
        <a-table-column key="endpoints" title="成员端点"
          ><template #default="{ record }">
            <div class="member-list">
              <span v-for="id in record.endpoint_ids" :key="id" class="member-item">
                {{ props.endpointById.get(id)?.name ?? `${id.slice(0, 8)}…` }}
                <small v-if="props.endpointById.get(id)">
                  {{ props.connectorById.get(props.endpointById.get(id)!.connector_id)?.name ?? '未知连接器' }}
                </small>
              </span>
              <span v-if="!record.endpoint_ids.length" class="wrap-value">未配置端点</span>
            </div>
          </template></a-table-column
        >
        <a-table-column key="version" title="版本" :width="80"
          ><template #default="{ record }"
            ><span class="mono-value">v{{ record.version }}</span></template
          ></a-table-column
        >
        <a-table-column key="enabled" title="状态" :width="100"
          ><template #default="{ record }"
            ><a-switch
              :checked="record.enabled"
              size="small"
              @change="(checked: boolean) => emit('toggle', record, checked)" /></template
        ></a-table-column>
        <a-table-column key="actions" title="操作" :width="116"
          ><template #default="{ record }"
            ><a-button type="text" aria-label="编辑职责组" @click="emit('edit', record)"
              ><template #icon><Pencil :size="15" /></template></a-button
            ><a-popconfirm
              title="确认删除此职责组？"
              ok-text="删除"
              cancel-text="取消"
              @confirm="emit('delete', record)"
              ><a-button type="text" danger aria-label="删除职责组"
                ><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></template
        ></a-table-column>
      </a-table>
    </div>
  </section>
</template>

<style scoped lang="scss">
.view-panel {
  min-width: 0;
}
.data-card-heading > div {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.data-card-heading h3 {
  margin: 0;
  font-size: var(--type-primary);
  letter-spacing: 0;
}
.panel-icon {
  color: var(--color-primary);
}
.data-table-wrap {
  overflow: hidden;
}
.wrap-value {
  display: block;
  max-width: 420px;
  overflow-wrap: anywhere;
  color: var(--text);
  font-size: var(--type-secondary);
  line-height: 1.45;
}
.dependency-strip {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 12px;
  color: var(--muted);
  font-size: var(--type-meta);
}
.dependency-step {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  padding: 6px 9px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
}
.dependency-step strong {
  color: var(--text);
  font: var(--type-secondary) var(--font-family-mono);
}
.dependency-alert {
  margin-bottom: 12px;
}
.member-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.member-item {
  display: inline-flex;
  flex-direction: column;
  padding: 4px 7px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface-hover);
  font-size: var(--type-secondary);
}
.member-item small {
  margin-top: 2px;
  color: var(--muted);
  font-size: var(--type-meta);
}
.mono-value {
  color: var(--muted);
  font: var(--type-meta) var(--font-family-mono);
}
</style>
