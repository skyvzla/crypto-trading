<script setup lang="ts">
import { Activity, Bell, Settings2, SlidersHorizontal, UsersRound } from 'lucide-vue-next'
import PageHeader from '@/features/operations/PageHeader.vue'
import NotificationActivityPanel from '@/features/notifications/NotificationActivityPanel.vue'
import NotificationConnectorsPanel from '@/features/notifications/NotificationConnectorsPanel.vue'
import NotificationDialogs from '@/features/notifications/NotificationDialogs.vue'
import NotificationGroupsPanel from '@/features/notifications/NotificationGroupsPanel.vue'
import NotificationOverviewPanel from '@/features/notifications/NotificationOverviewPanel.vue'
import NotificationPoliciesPanel from '@/features/notifications/NotificationPoliciesPanel.vue'
import { SEVERITY_OPTIONS, formatShortTime } from '@/features/notifications/presentation'
import { useNotificationWorkbench } from '@/features/notifications/useNotificationWorkbench'
import type { NotificationViewKey } from '@/features/notifications/types'

/**
 * 通知中心工作台。
 *
 * 各面板自行 import 展示映射（presentation.ts），这里只传数据与事件，
 * 不再把格式化函数当 props 往下透传。
 */
const workbench = useNotificationWorkbench()

const viewOptions: Array<{ key: NotificationViewKey; label: string; icon: typeof Bell }> = [
  { key: 'overview', label: '概览', icon: Activity },
  { key: 'connectors', label: '连接器与端点', icon: Settings2 },
  { key: 'groups', label: '职责组', icon: UsersRound },
  { key: 'policies', label: '路由策略', icon: SlidersHorizontal },
  { key: 'activity', label: '事件与投递', icon: Bell }
]

function changeView(key: string | number) {
  workbench.view.value = key as NotificationViewKey
}
function changeActivityView(key: NotificationViewKey | 'events' | 'deliveries') {
  workbench.activityView.value = key as 'events' | 'deliveries'
}
</script>

<template>
  <main class="notification-page">
    <PageHeader
      eyebrow="SYSTEM / NOTIFICATIONS"
      title="通知中心"
      description="统一管理故障、风控和交易信号的多渠道投递。"
      :loading="workbench.loading.value"
      :refreshed-at="workbench.lastSyncedAt.value ? formatShortTime(workbench.lastSyncedAt.value.toISOString()) : null"
      @refresh="workbench.loadAll"
    />

    <a-tabs :active-key="workbench.view.value" class="view-switcher" @change="changeView">
      <a-tab-pane v-for="item in viewOptions" :key="item.key">
        <template #tab>
          <span class="notification-tab-label">
            <component :is="item.icon" :size="15" />
            <span>{{ item.label }}</span>
            <a-badge v-if="item.key === 'activity' && workbench.deadDeliveryCount.value" :count="workbench.deadDeliveryCount.value" size="small" />
          </span>
        </template>
      </a-tab-pane>
    </a-tabs>

    <a-alert v-if="workbench.loadError.value" class="notification-alert" :message="workbench.loadError.value" type="warning" show-icon>
      <template #action><a-button type="link" size="small" @click="workbench.loadAll">重试</a-button></template>
    </a-alert>
    <div v-if="workbench.loading.value" class="query-state notification-loading">
      <a-spin size="small" /><span>正在读取通知配置…</span>
    </div>
    <template v-else>
      <NotificationOverviewPanel
        v-if="workbench.view.value === 'overview'"
        :overview="workbench.overview.value"
        :enabled-endpoint-count="workbench.enabledEndpointCount.value"
        :retry-delivery-count="workbench.retryDeliveryCount.value"
        :dead-delivery-count="workbench.deadDeliveryCount.value"
        :events="workbench.events.value"
        @open-activity="workbench.openActivity"
        @new-connector="workbench.resetConnectorForm(undefined, $event)"
        @new-policy="workbench.resetPolicyForm()"
      />
      <NotificationConnectorsPanel
        v-else-if="workbench.view.value === 'connectors'"
        :connectors="workbench.connectors.value"
        :endpoints="workbench.endpoints.value"
        :connector-by-id="workbench.connectorById.value"
        @new-connector="workbench.resetConnectorForm()"
        @new-endpoint="workbench.resetEndpointForm(undefined, $event)"
        @edit-connector="workbench.resetConnectorForm"
        @edit-endpoint="workbench.resetEndpointForm"
        @toggle-connector="workbench.toggleConnector"
        @toggle-endpoint="workbench.toggleEndpoint"
        @test-endpoint="workbench.testEndpoint"
        @delete-connector="workbench.deleteConnector"
        @delete-endpoint="workbench.deleteEndpoint"
      />
      <NotificationGroupsPanel
        v-else-if="workbench.view.value === 'groups'"
        :groups="workbench.groups.value"
        :endpoint-by-id="workbench.endpointById.value"
        @new="workbench.resetGroupForm()"
        @edit="workbench.resetGroupForm"
        @toggle="workbench.toggleGroup"
        @delete="workbench.deleteGroup"
      />
      <NotificationPoliciesPanel
        v-else-if="workbench.view.value === 'policies'"
        :policies="workbench.policies.value"
        :group-by-id="workbench.groupById.value"
        @new="workbench.resetPolicyForm()"
        @edit="workbench.resetPolicyForm"
        @toggle="workbench.togglePolicy"
        @delete="workbench.deletePolicy"
      />
      <NotificationActivityPanel
        v-else
        :activity-view="workbench.activityView.value"
        :events="workbench.events.value"
        :deliveries="workbench.deliveries.value"
        :dead-delivery-count="workbench.deadDeliveryCount.value"
        :event-filters="workbench.eventFilters"
        :delivery-filters="workbench.deliveryFilters"
        :activity-loading="workbench.activityLoading.value"
        :events-loading="workbench.eventsLoading.value"
        :deliveries-loading="workbench.deliveriesLoading.value"
        :endpoint-by-id="workbench.endpointById.value"
        :connector-by-id="workbench.connectorById.value"
        @update:activity-view="changeActivityView"
        @load="workbench.loadActivity"
        @apply-filters="workbench.applyActivityFilters"
        @retry-delivery="workbench.retryDelivery"
        @event-page="workbench.changeEventPage"
        @delivery-page="workbench.changeDeliveryPage"
      />
    </template>

    <NotificationDialogs
      :connector-open="workbench.connectorModalOpen.value"
      :endpoint-open="workbench.endpointModalOpen.value"
      :group-open="workbench.groupModalOpen.value"
      :policy-open="workbench.policyModalOpen.value"
      :saving="workbench.saving.value"
      :connector-form="workbench.connectorForm"
      :endpoint-form="workbench.endpointForm"
      :group-form="workbench.groupForm"
      :policy-form="workbench.policyForm"
      :connectors="workbench.connectors.value"
      :endpoints="workbench.endpoints.value"
      :groups="workbench.groups.value"
      :selected-connector="workbench.selectedConnector.value"
      :connector-by-id="workbench.connectorById.value"
      :connector-editing-id="workbench.connectorEditingId.value"
      :endpoint-editing-id="workbench.endpointEditingId.value"
      :group-editing-id="workbench.groupEditingId.value"
      :policy-editing-id="workbench.policyEditingId.value"
      :severity-options="SEVERITY_OPTIONS"
      @update:connector-open="workbench.connectorModalOpen.value = $event"
      @update:endpoint-open="workbench.endpointModalOpen.value = $event"
      @update:group-open="workbench.groupModalOpen.value = $event"
      @update:policy-open="workbench.policyModalOpen.value = $event"
      @submit-connector="workbench.submitConnector"
      @submit-endpoint="workbench.submitEndpoint"
      @submit-group="workbench.submitGroup"
      @submit-policy="workbench.submitPolicy"
    />
  </main>
</template>

<style scoped lang="scss">
/* 只保留页面骨架样式；各面板的样式已随组件一起下沉到组件内部。 */
.notification-page {
  width: 100%;
  min-width: 0;
  max-width: 1440px;
  margin: 0 auto;
  overflow-x: hidden;
}
.view-switcher :deep(.ant-tabs-nav) { margin: 0 0 15px; }
.view-switcher :deep(.ant-tabs-content-holder) { display: none; }
.notification-tab-label { display: inline-flex; align-items: center; gap: 7px; }
.notification-alert { margin-bottom: 14px; }

@media (max-width: 600px) {
  .view-switcher { margin-inline: -2px; }
}
</style>
