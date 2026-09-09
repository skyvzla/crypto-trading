<script setup lang="ts">
import { computed } from 'vue'
import { Globe2, MessageCircle } from 'lucide-vue-next'
import type { NotificationFormProps } from './types'

const props = defineProps<
  NotificationFormProps & {
    connectorOpen: boolean
    endpointOpen: boolean
    groupOpen: boolean
    policyOpen: boolean
    saving: boolean
    severityOptions: Array<{ value: string; label: string }>
  }
>()

const endpointGroups = computed(() => {
  const groups = new Map<string, { id: string; label: string; endpoints: NotificationFormProps['endpoints'] }>()
  for (const endpoint of props.endpoints) {
    const connector = props.connectorById.get(endpoint.connector_id)
    const group = groups.get(endpoint.connector_id) ?? {
      id: endpoint.connector_id,
      label: connector
        ? `${connector.name} · ${connector.type === 'telegram' ? 'Telegram Bot' : 'Webhook'}`
        : '未知连接器',
      endpoints: [],
    }
    group.endpoints.push(endpoint)
    groups.set(endpoint.connector_id, group)
  }
  return [...groups.values()]
})

function updateConnectorSecret(value: string) {
  props.connectorForm.secret = value
  if (value.trim()) props.connectorForm.clear_secret = false
}

function updateClearSecret(value: boolean) {
  props.connectorForm.clear_secret = value
  if (value) props.connectorForm.secret = ''
}

const emit = defineEmits<{
  'update:connectorOpen': [value: boolean]
  'update:endpointOpen': [value: boolean]
  'update:groupOpen': [value: boolean]
  'update:policyOpen': [value: boolean]
  'submit-connector': []
  'submit-endpoint': []
  'submit-group': []
  'submit-policy': []
}>()
</script>

<template>
  <a-modal
    :open="props.connectorOpen"
    :title="props.connectorEditingId ? '编辑连接器' : '新建连接器'"
    :confirm-loading="props.saving"
    ok-text="保存"
    cancel-text="取消"
    @update:open="emit('update:connectorOpen', $event)"
    @ok="emit('submit-connector')"
  >
    <a-form layout="vertical" class="modal-form">
      <a-form-item label="名称" required
        ><a-input v-model:value="props.connectorForm.name" :maxlength="128" placeholder="例如 ops-telegram"
      /></a-form-item>
      <a-form-item label="渠道类型" required
        ><a-radio-group v-model:value="props.connectorForm.type" :disabled="!!props.connectorEditingId"
          ><a-radio-button value="telegram"><MessageCircle :size="14" /> Telegram Bot</a-radio-button
          ><a-radio-button value="webhook"><Globe2 :size="14" /> Webhook</a-radio-button></a-radio-group
        ></a-form-item
      >
      <a-form-item
        :label="props.connectorForm.type === 'telegram' ? 'Telegram Bot token' : '密钥（可选）'"
        :required="props.connectorForm.type === 'telegram' && !props.connectorEditingId"
        ><a-input-password
          :value="props.connectorForm.secret"
          :disabled="props.connectorForm.clear_secret"
          autocomplete="new-password"
          @update:value="updateConnectorSecret"
          :placeholder="
            props.connectorEditingId
              ? '留空以保留现有 token'
              : props.connectorForm.type === 'telegram'
                ? '粘贴 BotFather 提供的 token'
                : '填写发送所需的密钥'
          "
      /></a-form-item>
      <a-checkbox
        v-if="props.connectorEditingId && props.connectorForm.type === 'webhook' && props.connectorForm.has_secret"
        :checked="props.connectorForm.clear_secret"
        @update:checked="updateClearSecret"
        >清除已保存密钥</a-checkbox
      >
      <div v-if="props.connectorForm.type === 'telegram'" class="form-grid">
        <a-form-item label="消息格式"
          ><a-select
            v-model:value="props.connectorForm.parse_mode"
            :options="[
              { value: 'HTML', label: 'HTML' },
              { value: 'MarkdownV2', label: 'MarkdownV2' },
            ]"
        /></a-form-item>
      </div>
      <div v-else class="form-grid">
        <a-form-item label="认证模式"
          ><a-select
            v-model:value="props.connectorForm.auth_type"
            :options="[
              { value: 'hmac_sha256', label: 'HMAC-SHA256' },
              { value: 'bearer', label: 'Bearer' },
              { value: 'none', label: '无认证' },
            ]" /></a-form-item
        ><a-form-item label="请求超时（秒）"
          ><a-input-number v-model:value="props.connectorForm.timeout_seconds" :min="1" :max="60"
        /></a-form-item>
      </div>
      <a-alert
        v-if="props.connectorForm.type === 'webhook' && props.connectorForm.auth_type !== 'none'"
        type="info"
        show-icon
        message="签名密钥通过安全配置保存；请求会携带版本化事件封装。"
      />
      <a-checkbox v-if="props.connectorForm.type === 'webhook'" v-model:checked="props.connectorForm.allow_http"
        >允许 HTTP（仅内网调试）</a-checkbox
      >
      <div class="form-meta">
        <span>配置版本 v{{ props.connectorForm.version }}</span
        ><a-switch v-model:checked="props.connectorForm.enabled" checked-children="启用" un-checked-children="停用" />
      </div>
    </a-form>
  </a-modal>

  <a-modal
    :open="props.endpointOpen"
    :title="props.endpointEditingId ? '编辑通知端点' : '添加通知端点'"
    :confirm-loading="props.saving"
    ok-text="保存"
    cancel-text="取消"
    @update:open="emit('update:endpointOpen', $event)"
    @ok="emit('submit-endpoint')"
  >
    <a-form layout="vertical" class="modal-form">
      <a-form-item label="所属连接器" required
        ><a-select
          v-model:value="props.endpointForm.connector_id"
          :disabled="!!props.endpointEditingId"
          placeholder="选择发送身份"
          :options="
            props.connectors.map((item) => ({
              value: item.id,
              label: `${item.name} · ${item.type === 'telegram' ? 'Telegram Bot' : 'Webhook'}`,
            }))
          "
      /></a-form-item>
      <a-form-item label="端点名称" required
        ><a-input v-model:value="props.endpointForm.name" :maxlength="128" placeholder="例如 risk-room"
      /></a-form-item>
      <a-form-item :label="props.selectedConnector?.type === 'telegram' ? 'Chat ID' : 'Webhook URL'" required
        ><a-input
          v-model:value="props.endpointForm.address"
          :placeholder="
            props.selectedConnector?.type === 'telegram' ? '-1001234567890' : 'https://hooks.example.com/notify'
          "
      /></a-form-item>
      <a-form-item v-if="props.selectedConnector?.type === 'telegram'" label="Topic ID（可选）"
        ><a-input v-model:value="props.endpointForm.topic_id" placeholder="论坛群组的 message_thread_id"
      /></a-form-item>
      <a-form-item v-else label="额外请求头（JSON）"
        ><a-textarea v-model:value="props.endpointForm.headers_json" :rows="4" spellcheck="false"
      /></a-form-item>
      <div class="form-meta">
        <span>配置版本 v{{ props.endpointForm.version }}</span
        ><a-switch v-model:checked="props.endpointForm.enabled" checked-children="启用" un-checked-children="停用" />
      </div>
    </a-form>
  </a-modal>

  <a-modal
    :open="props.groupOpen"
    :title="props.groupEditingId ? '编辑职责组' : '新建职责组'"
    :confirm-loading="props.saving"
    ok-text="保存"
    cancel-text="取消"
    @update:open="emit('update:groupOpen', $event)"
    @ok="emit('submit-group')"
  >
    <a-form layout="vertical" class="modal-form">
      <a-form-item label="职责组名称" required
        ><a-input v-model:value="props.groupForm.name" :maxlength="128" placeholder="例如 risk-oncall"
      /></a-form-item>
      <a-form-item label="说明"
        ><a-textarea v-model:value="props.groupForm.description" :rows="2" placeholder="说明该组负责的业务范围"
      /></a-form-item>
      <a-form-item label="成员端点" required
        ><a-select
          v-model:value="props.groupForm.endpoint_ids"
          mode="multiple"
          :disabled="!props.endpoints.length"
          placeholder="选择一个或多个接收端点"
        >
          <a-select-opt-group v-for="group in endpointGroups" :key="group.id" :label="group.label">
            <a-select-option v-for="item in group.endpoints" :key="item.id" :value="item.id">
              {{ item.name }} · {{ item.address }}
            </a-select-option>
          </a-select-opt-group>
        </a-select>
        <small v-if="!props.endpoints.length" class="field-hint">请关闭此弹窗并先添加连接器和端点。</small>
      </a-form-item>
      <div class="form-meta">
        <span>配置版本 v{{ props.groupForm.version }}</span
        ><a-switch v-model:checked="props.groupForm.enabled" checked-children="启用" un-checked-children="停用" />
      </div>
    </a-form>
  </a-modal>

  <a-modal
    :open="props.policyOpen"
    :title="props.policyEditingId ? '编辑路由策略' : '新建路由策略'"
    :confirm-loading="props.saving"
    ok-text="保存"
    cancel-text="取消"
    @update:open="emit('update:policyOpen', $event)"
    @ok="emit('submit-policy')"
  >
    <a-form layout="vertical" class="modal-form">
      <a-form-item label="策略名称" required
        ><a-input v-model:value="props.policyForm.name" :maxlength="128" placeholder="例如 critical-ops"
      /></a-form-item>
      <div class="form-grid">
        <a-form-item label="事件模式" required
          ><a-input v-model:value="props.policyForm.event_pattern" placeholder="risk.* 或精确事件名" /></a-form-item
        ><a-form-item label="重要级别" required
          ><a-select
            v-model:value="props.policyForm.severity"
            :options="props.severityOptions.filter((item) => item.value)"
        /></a-form-item>
      </div>
      <div class="form-grid">
        <a-form-item label="优先级"
          ><a-input-number v-model:value="props.policyForm.priority" :min="-999" :max="999" /></a-form-item
        ><a-form-item label="职责组"
          ><a-select
            v-model:value="props.policyForm.group_ids"
            mode="multiple"
            :disabled="props.policyForm.suppress"
            :options="props.groups.map((item) => ({ value: item.id, label: item.name }))"
            placeholder="选择通知职责组"
        /></a-form-item>
      </div>
      <div class="policy-toggle">
        <div><strong>显式抑制</strong><small>匹配事件只记录，不创建投递任务</small></div>
        <a-switch v-model:checked="props.policyForm.suppress" />
      </div>
      <div class="form-meta">
        <span>配置版本 v{{ props.policyForm.version }}</span
        ><a-switch v-model:checked="props.policyForm.enabled" checked-children="启用" un-checked-children="停用" />
      </div>
    </a-form>
  </a-modal>
</template>

<style scoped lang="scss">
.modal-form {
  padding-top: 5px;
}
.modal-form :deep(.ant-form-item) {
  margin-bottom: 14px;
}
.modal-form :deep(.ant-radio-button-wrapper) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.modal-form :deep(.ant-input-number),
.modal-form :deep(.ant-select) {
  width: 100%;
}
.field-hint {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: var(--type-meta);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.form-meta,
.policy-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 36px;
  padding-top: 7px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font: var(--type-meta) var(--font-family-mono);
}
.policy-toggle {
  margin-bottom: 12px;
  padding: 8px 0;
  border-top: 0;
}
.policy-toggle strong,
.policy-toggle small {
  display: block;
  font-family: var(--font-family-sans);
}
.policy-toggle strong {
  color: var(--text);
  font-size: var(--type-secondary);
}
.policy-toggle small {
  margin-top: 3px;
  color: var(--muted);
  font-size: var(--type-meta);
}

@media (max-width: 600px) {
  .form-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
