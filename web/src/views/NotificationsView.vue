<script setup lang="ts">
import { Activity, Bell, RefreshCw, Settings2, SlidersHorizontal, UsersRound } from 'lucide-vue-next'
import NotificationActivityPanel from '@/features/notifications/NotificationActivityPanel.vue'
import NotificationConnectorsPanel from '@/features/notifications/NotificationConnectorsPanel.vue'
import NotificationDialogs from '@/features/notifications/NotificationDialogs.vue'
import NotificationGroupsPanel from '@/features/notifications/NotificationGroupsPanel.vue'
import NotificationOverviewPanel from '@/features/notifications/NotificationOverviewPanel.vue'
import NotificationPoliciesPanel from '@/features/notifications/NotificationPoliciesPanel.vue'
import { useNotificationWorkbench } from '@/features/notifications/useNotificationWorkbench'
import type { NotificationViewKey } from '@/features/notifications/types'

const workbench = useNotificationWorkbench()
const {
  view,
  activityView,
  loading,
  activityLoading,
  saving,
  lastSyncedAt,
  loadError,
  connectors,
  endpoints,
  groups,
  policies,
  overview,
  events,
  deliveries,
  eventFilters,
  deliveryFilters,
  connectorModalOpen,
  endpointModalOpen,
  groupModalOpen,
  policyModalOpen,
  connectorEditingId,
  endpointEditingId,
  groupEditingId,
  policyEditingId,
  connectorForm,
  endpointForm,
  groupForm,
  policyForm,
  severityOptions,
  connectorById,
  endpointById,
  enabledEndpointCount,
  deadDeliveryCount,
  retryDeliveryCount,
  selectedConnector,
  loadAll,
  loadActivity,
  formatDate,
  formatLongDate,
  connectorLabel,
  severityLabel,
  statusLabel,
  statusBadge,
  endpointNames,
  groupNames,
  connectorConfigSummary,
  endpointConfigSummary,
  resetConnectorForm,
  resetEndpointForm,
  resetGroupForm,
  resetPolicyForm,
  submitConnector,
  submitEndpoint,
  submitGroup,
  submitPolicy,
  toggleConnector,
  toggleEndpoint,
  toggleGroup,
  togglePolicy,
  deleteConnector,
  deleteEndpoint,
  deleteGroup,
  deletePolicy,
  testEndpoint,
  retryDelivery,
  changeEventPage,
  changeDeliveryPage,
  navigateView,
  openActivity,
  setActivityView,
  openConnectorForm,
  openEndpointForm,
  setConnectorModalOpen,
  setEndpointModalOpen,
  setGroupModalOpen,
  setPolicyModalOpen
} = workbench

const viewOptions: Array<{ key: NotificationViewKey; label: string; icon: typeof Bell }> = [
  { key: 'overview', label: '概览', icon: Activity },
  { key: 'connectors', label: '连接器与端点', icon: Settings2 },
  { key: 'groups', label: '职责组', icon: UsersRound },
  { key: 'policies', label: '路由策略', icon: SlidersHorizontal },
  { key: 'activity', label: '事件与投递', icon: Bell }
]
function changeView(key: string | number) { navigateView(key as NotificationViewKey) }
</script>

<template>
  <main class="notification-page">
    <header class="notification-heading">
      <div class="heading-copy"><span class="eyebrow">SYSTEM / NOTIFICATIONS</span><div class="title-line"><span class="title-icon"><Bell :size="19" /></span><div><h1>通知中心</h1><p>统一管理故障、风控和交易信号的多渠道投递。</p></div></div></div>
      <div class="heading-actions"><span v-if="lastSyncedAt" class="sync-stamp">同步于 {{ formatDate(lastSyncedAt.toISOString()) }}</span><a-button class="icon-button" :loading="loading" aria-label="刷新通知数据" @click="loadAll"><template #icon><RefreshCw :size="15" /></template>刷新</a-button></div>
    </header>

    <a-tabs :active-key="view" class="view-switcher" @change="changeView">
      <a-tab-pane v-for="item in viewOptions" :key="item.key">
        <template #tab><span class="notification-tab-label"><component :is="item.icon" :size="15" /><span>{{ item.label }}</span><a-badge v-if="item.key === 'activity' && deadDeliveryCount" :count="deadDeliveryCount" size="small" /></span></template>
      </a-tab-pane>
    </a-tabs>

    <a-alert v-if="loadError" class="notification-alert" :message="loadError" type="warning" show-icon><template #action><a-button type="link" size="small" @click="loadAll">重试</a-button></template></a-alert>
    <div v-if="loading" class="query-state notification-loading"><a-spin size="small" /><span>正在读取通知配置…</span></div>
    <template v-else>
      <NotificationOverviewPanel v-if="view === 'overview'" :overview="overview" :enabled-endpoint-count="enabledEndpointCount" :retry-delivery-count="retryDeliveryCount" :dead-delivery-count="deadDeliveryCount" :events="events" :format-date="formatDate" :status-label="statusLabel" :status-badge="statusBadge" @open-activity="openActivity" @new-connector="openConnectorForm" @new-policy="resetPolicyForm" />
      <NotificationConnectorsPanel v-else-if="view === 'connectors'" :connectors="connectors" :endpoints="endpoints" :connector-label="connectorLabel" :connector-config-summary="connectorConfigSummary" :endpoint-config-summary="endpointConfigSummary" @new-connector="openConnectorForm" @new-endpoint="openEndpointForm" @edit-connector="resetConnectorForm" @edit-endpoint="resetEndpointForm" @toggle-connector="toggleConnector" @toggle-endpoint="toggleEndpoint" @test-endpoint="testEndpoint" @delete-connector="deleteConnector" @delete-endpoint="deleteEndpoint" />
      <NotificationGroupsPanel v-else-if="view === 'groups'" :groups="groups" :endpoint-names="endpointNames" @new="resetGroupForm" @edit="resetGroupForm" @toggle="toggleGroup" @delete="deleteGroup" />
      <NotificationPoliciesPanel v-else-if="view === 'policies'" :policies="policies" :group-names="groupNames" :severity-label="severityLabel" :status-badge="statusBadge" @new="resetPolicyForm" @edit="resetPolicyForm" @toggle="togglePolicy" @delete="deletePolicy" />
      <NotificationActivityPanel v-else :activity-view="activityView" :events="events" :deliveries="deliveries" :dead-delivery-count="deadDeliveryCount" :event-filters="eventFilters" :delivery-filters="deliveryFilters" :severity-options="severityOptions" :activity-loading="activityLoading" :endpoint-by-id="endpointById" :connector-by-id="connectorById" :status-label="statusLabel" :severity-label="severityLabel" :status-badge="statusBadge" :format-long-date="formatLongDate" @update:activity-view="setActivityView" @load="loadActivity" @retry-delivery="retryDelivery" @event-page="changeEventPage" @delivery-page="changeDeliveryPage" />
    </template>

    <NotificationDialogs :connector-open="connectorModalOpen" :endpoint-open="endpointModalOpen" :group-open="groupModalOpen" :policy-open="policyModalOpen" :saving="saving" :connector-form="connectorForm" :endpoint-form="endpointForm" :group-form="groupForm" :policy-form="policyForm" :connectors="connectors" :endpoints="endpoints" :groups="groups" :selected-connector="selectedConnector" :connector-by-id="connectorById" :connector-editing-id="connectorEditingId" :endpoint-editing-id="endpointEditingId" :group-editing-id="groupEditingId" :policy-editing-id="policyEditingId" :severity-options="severityOptions" @update:connector-open="setConnectorModalOpen" @update:endpoint-open="setEndpointModalOpen" @update:group-open="setGroupModalOpen" @update:policy-open="setPolicyModalOpen" @submit-connector="submitConnector" @submit-endpoint="submitEndpoint" @submit-group="submitGroup" @submit-policy="submitPolicy" />
  </main>
</template>

<style lang="scss">
.notification-page {
  width: 100%; min-width: 0; max-width: 1440px; margin: 0 auto; overflow-x: hidden;
.notification-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; padding-bottom: 15px; border-bottom: 1px solid var(--line); }
.heading-copy { min-width: 0; }
.eyebrow, .section-kicker { display: block; color: var(--color-gold); font: var(--font-size-xs)/1.3 var(--font-family-mono); letter-spacing: 0; }
.title-line { display: flex; align-items: center; gap: 11px; margin-top: 6px; }
.title-icon { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid color-mix(in srgb, var(--color-primary) 35%, transparent); border-radius: 6px; color: var(--color-primary); background: color-mix(in srgb, var(--color-primary) 8%, transparent); }
h1 { margin: 0; font-size: var(--font-size-display); line-height: 1.2; letter-spacing: 0; }
.title-line p { margin: 4px 0 0; color: var(--muted); font-size: var(--font-size-sm); }
.heading-actions, .section-actions { display: flex; align-items: center; gap: 8px; }
.sync-stamp { color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); white-space: nowrap; }
.icon-button { display: inline-flex; align-items: center; gap: 6px; }
.view-switcher .ant-tabs-nav { margin: 0 0 15px; }
.view-switcher .ant-tabs-content-holder { display: none; }
.notification-tab-label { display: inline-flex; align-items: center; gap: 7px; font-size: var(--font-size-sm); }
.notification-alert { margin-bottom: 14px; }
.query-state, .query-empty { display: flex; align-items: center; justify-content: center; min-height: 220px; gap: 10px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); background: var(--surface); }
.view-panel { min-width: 0; }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 10px; }
.section-heading h2 { margin: 4px 0 0; font-size: var(--font-size-page-title); line-height: 1.25; letter-spacing: 0; }
.section-note { color: var(--muted); font-size: var(--font-size-xs); }
.section-description { margin: 0 0 14px; color: var(--muted); font-size: var(--font-size-sm); }
.metric-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 14px; overflow: hidden; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.metric-item { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }
.metric-item:last-child { border-right: 0; }
.metric-item > span, .metric-item em { display: block; color: var(--muted); font-size: var(--font-size-xs); font-style: normal; }
.metric-item strong { display: block; margin: 5px 0 3px; color: var(--text); font: 600 var(--font-size-display)/1 var(--font-family-mono); }
.metric-item strong small { margin-left: 2px; color: var(--muted); font-size: var(--font-size-md); font-weight: 400; }
.metric-warning strong { color: var(--color-warning); }
.overview-grid { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); gap: 14px; margin-bottom: 14px; }
.surface-panel { border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 43px; padding: 10px 13px; border-bottom: 1px solid var(--line); }
.panel-heading > div { display: flex; align-items: center; gap: 7px; min-width: 0; }
.panel-heading h3 { margin: 0; font-size: var(--font-size-md); letter-spacing: 0; }
.panel-icon { color: var(--color-primary); }
.quiet-value { color: var(--muted); font-size: var(--font-size-xs); }
.health-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 4px 13px 7px; }
.health-list > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 0; border-bottom: 1px solid color-mix(in srgb, var(--muted) 16%, transparent); color: var(--muted); font-size: var(--font-size-sm); }
.health-list > div:nth-last-child(-n+2) { border-bottom: 0; }
.health-list strong { color: var(--text); font: var(--font-size-sm) var(--font-family-mono); }
.health-list span { display: inline-flex; align-items: center; gap: 7px; }
.panel-link { display: inline-flex; align-items: center; gap: 5px; padding: 6px 13px 12px; font-size: var(--font-size-xs); }
.panel-link span { font-size: var(--font-size-lg); line-height: 10px; }
.quick-panel { padding-bottom: 3px; }
.quick-action { display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 9px; width: 100%; min-height: 50px; padding: 7px 13px; border: 0; border-bottom: 1px solid color-mix(in srgb, var(--muted) 15%, transparent); color: var(--text); background: transparent; text-align: left; cursor: pointer; }
.quick-action:last-child { border-bottom: 0; }.quick-action:hover { background: var(--surface-hover); }.quick-action > span:nth-child(2) { min-width: 0; }.quick-action strong, .quick-action small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.quick-action strong { font-size: var(--font-size-sm); }.quick-action small { margin-top: 2px; color: var(--muted); font-size: var(--font-size-xs); }.quick-mark { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 6px; }.quick-mark.telegram { color: var(--color-info); background: color-mix(in srgb, var(--color-info) 10%, transparent); }.quick-mark.webhook { color: #0f766e; background: rgba(13, 148, 136, .1); }.quick-mark.policy { color: var(--color-warning); background: color-mix(in srgb, var(--color-warning) 10%, transparent); }.quick-arrow { color: var(--muted); font-size: var(--font-size-lg); }
.recent-panel { overflow: hidden; }.event-list { padding: 3px 13px; }.event-row { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto 128px; align-items: center; gap: 9px; min-height: 47px; border-bottom: 1px solid color-mix(in srgb, var(--muted) 15%, transparent); }.event-row:last-child { border-bottom: 0; }.event-severity { display: grid; place-items: center; }.event-copy { min-width: 0; }.event-copy strong, .event-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.event-copy strong { font-size: var(--font-size-sm); }.event-copy small { margin-top: 2px; color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); }.event-row time { color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); text-align: right; white-space: nowrap; }
.connector-list { display: grid; gap: 12px; }.connector-block { overflow: hidden; }.connector-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 14px 11px; }.connector-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }.connector-mark { display: grid; flex: 0 0 auto; place-items: center; width: 32px; height: 32px; border-radius: 6px; }.connector-mark.telegram { color: var(--color-info); background: color-mix(in srgb, var(--color-info) 10%, transparent); }.connector-mark.webhook { color: #0f766e; background: rgba(13, 148, 136, .1); }.connector-name { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }.connector-name strong { overflow-wrap: anywhere; font-size: var(--font-size-md); }.connector-identity small { display: block; margin-top: 3px; color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); overflow-wrap: anywhere; }.connector-actions { display: flex; align-items: center; gap: 4px; }.endpoint-head { display: flex; align-items: center; justify-content: space-between; padding: 7px 14px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); color: var(--muted); font-size: var(--font-size-xs); text-transform: uppercase; letter-spacing: 0; }.endpoint-head > div { display: flex; align-items: center; gap: 8px; }.endpoint-head .ant-btn { height: 24px; padding-inline: 4px; font-size: var(--font-size-xs); text-transform: none; letter-spacing: 0; }.endpoint-list { padding: 0 14px; }.endpoint-row { display: grid; grid-template-columns: 9px minmax(0, 1fr) auto 34px 30px 30px 30px; align-items: center; gap: 9px; min-height: 50px; border-bottom: 1px solid color-mix(in srgb, var(--muted) 15%, transparent); }.endpoint-row:last-child { border-bottom: 0; }.endpoint-state { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }.endpoint-state.enabled { background: var(--color-success); }.endpoint-copy { min-width: 0; }.endpoint-copy strong, .endpoint-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.endpoint-copy strong { font-size: var(--font-size-sm); }.endpoint-copy small { margin-top: 2px; color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); }.endpoint-version, .mono-value { color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); }.empty-inline { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 14px; color: var(--muted); font-size: var(--font-size-sm); }.data-table-wrap { overflow: hidden; }.data-table-wrap .ant-table-thead > tr > th { color: var(--muted); background: var(--surface-hover); font-size: var(--font-size-xs); font-weight: 500; }.data-table-wrap .ant-table-tbody > tr > td, .activity-table .ant-table-tbody > tr > td { border-bottom-color: color-mix(in srgb, var(--muted) 15%, transparent); }.primary-cell { min-width: 0; }.primary-cell strong, .primary-cell small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.primary-cell strong { font-size: var(--font-size-sm); }.primary-cell small { margin-top: 3px; color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); }.wrap-value { display: block; max-width: 420px; overflow-wrap: anywhere; color: var(--text); font-size: var(--font-size-sm); line-height: 1.45; }.table-empty { padding: 30px 0; }.pattern-code { display: inline-block; max-width: 260px; padding: 3px 6px; border: 1px solid var(--line); border-radius: 4px; color: var(--text); background: var(--surface-hover); font: var(--font-size-xs) var(--font-family-mono); overflow-wrap: anywhere; }
.activity-switcher .ant-tabs-nav { margin: 0 0 10px; }
.activity-switcher .ant-tabs-content-holder { display: none; }
.activity-tab-label { display: inline-flex; align-items: center; gap: 7px; font-size: var(--font-size-sm); }
.activity-table { overflow: hidden; }.filter-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 11px 12px; border-bottom: 1px solid var(--line); }.filter-row .ant-input { width: min(290px, 100%); }.filter-row .ant-select { min-width: 125px; }.table-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 51px; padding: 8px 13px; border-top: 1px solid var(--line); color: var(--muted); font-size: var(--font-size-xs); }.muted-dash { color: var(--muted); }
@media (max-width: 900px) { .notification-heading, .section-heading { align-items: flex-start; flex-direction: column; }.heading-actions { width: 100%; justify-content: space-between; }.metric-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }.metric-item:nth-child(2) { border-right: 0; }.metric-item:nth-child(-n+2) { border-bottom: 1px solid var(--line); }.overview-grid { grid-template-columns: 1fr; }.section-actions { width: 100%; }.section-actions .ant-btn { flex: 1; }.event-row { grid-template-columns: 12px minmax(0, 1fr) auto; }.event-row time { display: none; }.data-table-wrap .ant-table { min-width: 700px; }.data-table-wrap { overflow-x: auto; }.data-table-wrap .ant-table-wrapper { min-width: 700px; } }
@media (max-width: 600px) { .notification-heading { gap: 12px; }.title-line p { max-width: 270px; line-height: 1.45; }.view-switcher { margin-inline: -2px; }.section-note { display: none; }.metric-item { padding: 12px; }.metric-item strong { font-size: var(--font-size-page-title); }.health-list { grid-template-columns: 1fr; }.health-list > div:nth-last-child(-n+2) { border-bottom: 1px solid color-mix(in srgb, var(--muted) 16%, transparent); }.health-list > div:last-child { border-bottom: 0; }.connector-header { align-items: flex-start; }.connector-actions { flex: 0 0 auto; }.endpoint-row { grid-template-columns: 9px minmax(0, 1fr) repeat(4, 30px); gap: 5px; }.endpoint-version { display: none; }.filter-row > .ant-input, .filter-row > .ant-select, .filter-row > .ant-btn { width: 100% !important; }.table-footer { align-items: flex-start; flex-direction: column; } }
}
</style>
