<script setup lang="ts">
import { Activity, RefreshCw, RotateCcw, SlidersHorizontal } from 'lucide-vue-next'
import type { NotificationConnector, NotificationDelivery, NotificationEndpoint, NotificationEvent, Page } from '@/api/types'
import type { NotificationActivityKey } from './types'
import { SEVERITY_OPTIONS, formatFullTime, severityLabel, statusBadge, statusLabel } from './presentation'
import NotificationSectionHeader from './NotificationSectionHeader.vue'

const props = defineProps<{
  activityView: NotificationActivityKey
  events: Page<NotificationEvent>
  deliveries: Page<NotificationDelivery>
  deadDeliveryCount: number
  eventFilters: { event_type: string; severity: string; routing_status: string }
  deliveryFilters: { status: string; endpoint_id: string; event_id: string }
  activityLoading: boolean
  eventsLoading: boolean
  deliveriesLoading: boolean
  endpointById: Map<string, NotificationEndpoint>
  connectorById: Map<string, NotificationConnector>
}>()

const severityOptions = SEVERITY_OPTIONS

const emit = defineEmits<{
  'update:activityView': [value: NotificationActivityKey]
  /** 刷新当前那张表，保持页码。 */
  load: []
  /** 应用筛选条件，回到第一页。 */
  'apply-filters': []
  'retry-delivery': [item: NotificationDelivery]
  'event-page': [page: number]
  'delivery-page': [page: number]
}>()

function changeActivityView(key: string | number) {
  emit('update:activityView', key as NotificationActivityKey)
}
</script>

<template>
  <section class="view-panel activity-view" aria-labelledby="activity-heading">
    <NotificationSectionHeader id="activity-heading" kicker="DELIVERY LEDGER" title="事件与投递">
      <template #actions><a-button @click="emit('load')" :loading="activityLoading"><template #icon><RefreshCw :size="15" /></template>刷新队列</a-button></template>
    </NotificationSectionHeader>
    <a-tabs :active-key="activityView" class="activity-switcher" @change="changeActivityView">
      <a-tab-pane key="events"><template #tab><span class="activity-tab-label">事件<a-badge :count="events.total" show-zero color="var(--color-info)" /></span></template></a-tab-pane>
      <a-tab-pane key="deliveries"><template #tab><span class="activity-tab-label">投递<a-badge :count="deliveries.total" show-zero :color="deadDeliveryCount > 0 ? 'var(--color-danger)' : 'var(--color-info)'" /></span></template></a-tab-pane>
    </a-tabs>

    <section v-if="activityView === 'events'" class="activity-table data-card">
      <div class="filter-row"><a-input v-model:value="eventFilters.event_type" allow-clear placeholder="事件类型，例如 risk.halted" @press-enter="emit('apply-filters')"><template #prefix><Activity :size="14" /></template></a-input><a-select v-model:value="eventFilters.severity" :options="severityOptions" style="width: 130px" @change="emit('apply-filters')" /><a-select v-model:value="eventFilters.routing_status" allow-clear placeholder="路由状态" style="width: 130px" @change="emit('apply-filters')" :options="[{ value: 'routed', label: '已路由' }, { value: 'unrouted', label: '未匹配' }, { value: 'suppressed', label: '已抑制' }, { value: 'targeted', label: '定向测试' }]" /><a-button aria-label="应用事件筛选" @click="emit('apply-filters')"><template #icon><SlidersHorizontal :size="14" /></template>筛选</a-button></div>
      <a-table :data-source="events.items" :pagination="false" :scroll="{ x: 700 }" row-key="id" size="small" :loading="eventsLoading"><a-table-column key="event" title="事件"><template #default="{ record }"><div class="primary-cell"><strong>{{ record.title }}</strong><small>{{ record.event_type }} · {{ record.source }}</small></div></template></a-table-column><a-table-column key="severity" title="级别" :width="88"><template #default="{ record }"><a-badge :status="statusBadge(record.severity)" :text="severityLabel(record.severity)" /></template></a-table-column><a-table-column key="route" title="路由" :width="100"><template #default="{ record }"><a-badge :status="statusBadge(record.routing_status)" :text="statusLabel(record.routing_status)" /></template></a-table-column><a-table-column key="occurred" title="发生时间" :width="150"><template #default="{ record }"><time class="mono-value">{{ formatFullTime(record.occurred_at) }}</time></template></a-table-column></a-table>
      <div class="table-footer"><span>共 {{ events.total }} 条事件</span><a-pagination size="small" :current="Math.floor(events.offset / events.limit) + 1" :page-size="events.limit" :total="events.total" :show-size-changer="false" @change="(page: number) => emit('event-page', page)" /></div>
    </section>

    <section v-else class="activity-table data-card">
      <div class="filter-row"><a-select v-model:value="deliveryFilters.status" allow-clear placeholder="投递状态" style="width: 130px" :options="[{ value: 'pending', label: '待发送' }, { value: 'retry', label: '待重试' }, { value: 'sent', label: '已发送' }, { value: 'dead', label: '死信' }]" @change="emit('apply-filters')" /><a-input v-model:value="deliveryFilters.endpoint_id" allow-clear placeholder="端点 ID" @press-enter="emit('apply-filters')" /><a-input v-model:value="deliveryFilters.event_id" allow-clear placeholder="事件 ID" @press-enter="emit('apply-filters')" /><a-button aria-label="应用投递筛选" @click="emit('apply-filters')"><template #icon><SlidersHorizontal :size="14" /></template>筛选</a-button></div>
      <a-table :data-source="deliveries.items" :pagination="false" :scroll="{ x: 700 }" row-key="id" size="small" :loading="deliveriesLoading"><a-table-column key="delivery" title="投递"><template #default="{ record }"><div class="primary-cell"><strong>{{ endpointById.get(record.endpoint_id)?.name ?? record.endpoint_id.slice(0, 12) + '…' }}</strong><small>{{ connectorById.get(endpointById.get(record.endpoint_id)?.connector_id ?? '')?.name ?? '快照连接器' }}</small></div></template></a-table-column><a-table-column key="status" title="状态" :width="100"><template #default="{ record }"><a-badge :status="statusBadge(record.status)" :text="statusLabel(record.status)" /></template></a-table-column><a-table-column key="attempts" title="尝试" :width="74"><template #default="{ record }"><span class="mono-value">{{ record.attempt_count }}</span></template></a-table-column><a-table-column key="updated" title="更新时间" :width="150"><template #default="{ record }"><time class="mono-value">{{ formatFullTime(record.updated_at) }}</time></template></a-table-column><a-table-column key="actions" title="操作" :width="86"><template #default="{ record }"><a-button v-if="record.status === 'dead' || record.status === 'retry'" type="link" size="small" @click="emit('retry-delivery', record)"><template #icon><RotateCcw :size="14" /></template>重试</a-button><span v-else class="muted-dash">—</span></template></a-table-column></a-table>
      <div class="table-footer"><span>共 {{ deliveries.total }} 条投递</span><a-pagination size="small" :current="Math.floor(deliveries.offset / deliveries.limit) + 1" :page-size="deliveries.limit" :total="deliveries.total" :show-size-changer="false" @change="(page: number) => emit('delivery-page', page)" /></div>
    </section>
  </section>
</template>

<style scoped lang="scss">
.view-panel { min-width: 0; }
.data-card-heading > div { display: flex; align-items: center; gap: 7px; min-width: 0; }
.data-card-heading h3 { margin: 0; font-size: var(--type-primary); letter-spacing: 0; }
.panel-icon { color: var(--color-primary); }
.activity-switcher :deep(.ant-tabs-nav) { margin: 0 0 10px; }
.activity-switcher :deep(.ant-tabs-content-holder) { display: none; }
.activity-tab-label { display: inline-flex; align-items: center; gap: 7px; }
.activity-table { overflow: hidden; }
.mono-value { color: var(--muted); font: var(--type-meta) var(--font-family-mono); }
.filter-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 11px 12px; border-bottom: 1px solid var(--line); }
.filter-row :deep(.ant-input) { width: min(290px, 100%); }
.filter-row :deep(.ant-select) { min-width: 125px; }
.table-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 51px; padding: 8px 13px; border-top: 1px solid var(--line); color: var(--muted); font-size: var(--type-meta); }
.muted-dash { color: var(--muted); }

@media (max-width: 600px) {
  .filter-row > :deep(.ant-input), .filter-row > :deep(.ant-select), .filter-row > :deep(.ant-btn) { width: 100% !important; }
  .table-footer { align-items: flex-start; flex-direction: column; }
}
</style>
