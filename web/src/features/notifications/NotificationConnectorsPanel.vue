<script setup lang="ts">
import { Globe2, MessageCircle, Pencil, Plus, Send, Trash2 } from 'lucide-vue-next'
import { computed } from 'vue'
import type { NotificationConnector, NotificationEndpoint } from '@/api/types'

const props = defineProps<{
  connectors: NotificationConnector[]
  endpoints: NotificationEndpoint[]
  connectorLabel: (type: string) => string
  connectorConfigSummary: (item: NotificationConnector) => string
  endpointConfigSummary: (item: NotificationEndpoint) => string
}>()

const emit = defineEmits<{
  'new-connector': []
  'new-endpoint': [connectorId?: string]
  'edit-connector': [item: NotificationConnector]
  'edit-endpoint': [item: NotificationEndpoint]
  'toggle-connector': [item: NotificationConnector, enabled: boolean]
  'toggle-endpoint': [item: NotificationEndpoint, enabled: boolean]
  'test-endpoint': [item: NotificationEndpoint]
  'delete-connector': [item: NotificationConnector]
  'delete-endpoint': [item: NotificationEndpoint]
}>()

const endpointsByConnector = computed(() => {
  const result = new Map<string, NotificationEndpoint[]>()
  for (const endpoint of props.endpoints) {
    const items = result.get(endpoint.connector_id) ?? []
    items.push(endpoint)
    result.set(endpoint.connector_id, items)
  }
  return result
})
</script>

<template>
  <section class="view-panel" aria-labelledby="connectors-heading">
    <div class="section-heading"><div><span class="section-kicker">CHANNEL FABRIC</span><h2 id="connectors-heading">连接器与端点</h2></div><div class="section-actions"><a-button @click="emit('new-endpoint')"><template #icon><Plus :size="15" /></template>添加端点</a-button><a-button type="primary" @click="emit('new-connector')"><template #icon><Plus :size="15" /></template>添加连接器</a-button></div></div>
    <p class="section-description">连接器代表发送身份与公共鉴权；端点代表具体 Telegram chat / topic 或 Webhook URL。</p>
    <div class="connector-list">
      <section v-for="connector in connectors" :key="connector.id" class="connector-block surface-panel">
        <div class="connector-header"><div class="connector-identity"><span class="connector-mark" :class="connector.type"><MessageCircle v-if="connector.type === 'telegram'" :size="17" /><Globe2 v-else :size="17" /></span><div><div class="connector-name"><strong>{{ connector.name }}</strong><a-tag :color="connector.type === 'telegram' ? 'blue' : 'cyan'">{{ connectorLabel(connector.type) }}</a-tag></div><small>{{ connectorConfigSummary(connector) }} · v{{ connector.version }}</small></div></div><div class="connector-actions"><a-switch :checked="connector.enabled" size="small" @change="(checked: boolean) => emit('toggle-connector', connector, checked)" /><a-button type="text" aria-label="编辑连接器" @click="emit('edit-connector', connector)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此连接器？必须先移除所有端点。" ok-text="删除" cancel-text="取消" @confirm="emit('delete-connector', connector)"><a-button type="text" danger aria-label="删除连接器"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></div></div>
        <div class="endpoint-head"><span>端点</span><div><span>{{ endpointsByConnector.get(connector.id)?.length ?? 0 }} 个地址</span><a-button type="link" size="small" @click="emit('new-endpoint', connector.id)"><template #icon><Plus :size="13" /></template>{{ connector.type === 'webhook' ? '添加 URL' : '添加 Chat / Topic' }}</a-button></div></div>
        <div v-if="endpointsByConnector.get(connector.id)?.length" class="endpoint-list"><div v-for="endpoint in endpointsByConnector.get(connector.id)" :key="endpoint.id" class="endpoint-row"><span class="endpoint-state" :class="{ enabled: endpoint.enabled }" /><div class="endpoint-copy"><strong>{{ endpoint.name }}</strong><small>{{ endpointConfigSummary(endpoint) }}</small></div><span class="endpoint-version">v{{ endpoint.version }}</span><a-switch :checked="endpoint.enabled" size="small" @change="(checked: boolean) => emit('toggle-endpoint', endpoint, checked)" /><a-tooltip title="发送测试通知"><a-button type="text" aria-label="发送测试通知" @click="emit('test-endpoint', endpoint)"><template #icon><Send :size="15" /></template></a-button></a-tooltip><a-button type="text" aria-label="编辑端点" @click="emit('edit-endpoint', endpoint)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此通知端点？" ok-text="删除" cancel-text="取消" @confirm="emit('delete-endpoint', endpoint)"><a-button type="text" danger aria-label="删除端点"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></div></div>
        <div v-else class="empty-inline"><span>该连接器还没有接收端点</span><a-button type="link" @click="emit('new-endpoint', connector.id)">添加第一个端点</a-button></div>
      </section>
      <div v-if="!connectors.length" class="query-empty"><a-empty description="尚未配置连接器"><template #extra><a-button type="primary" @click="emit('new-connector')"><template #icon><Plus :size="15" /></template>创建连接器</a-button></template></a-empty></div>
    </div>
  </section>
</template>
