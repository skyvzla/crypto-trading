<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown, RefreshCw, RotateCcw, Search, SlidersHorizontal } from 'lucide-vue-next'
import type {
  NotificationConnector,
  NotificationDelivery,
  NotificationEndpoint,
  NotificationEvent,
  Page,
} from '@/api/types'
import type { NotificationActivityKey } from './types'
import { SEVERITY_OPTIONS, formatFullTime, severityLabel, statusBadge, statusLabel } from './presentation'
import NotificationSectionHeader from './NotificationSectionHeader.vue'

const props = defineProps<{
  activityView: NotificationActivityKey
  events: Page<NotificationEvent>
  deliveries: Page<NotificationDelivery>
  deadDeliveryCount: number
  eventFilters: { q: string; event_type: string; source: string; severity: string; routing_status: string }
  deliveryFilters: { q: string; status: string; endpoint_id: string; event_id: string }
  activityLoading: boolean
  eventsLoading: boolean
  deliveriesLoading: boolean
  endpointById: Map<string, NotificationEndpoint>
  connectorById: Map<string, NotificationConnector>
}>()

const severityOptions = SEVERITY_OPTIONS
const expandedEventId = ref<string | null>(null)
const eventFiltersExpanded = ref(false)
const deliveryFiltersExpanded = ref(false)
const expandedEvent = computed(() => props.events.items.find((event) => event.id === expandedEventId.value) ?? null)
const endpointOptions = computed(() =>
  [...props.endpointById.values()].map((endpoint) => ({
    value: endpoint.id,
    label: `${endpoint.name} · ${props.connectorById.get(endpoint.connector_id)?.name ?? '未知连接器'}`,
  })),
)
type FocusableInput = { focus: () => void }
const eventSearchInput = ref<FocusableInput | null>(null)
const deliverySearchInput = ref<FocusableInput | null>(null)

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

function focusSearchInput(input: FocusableInput | null, event: MouseEvent) {
  const shell = event.currentTarget
  if (shell instanceof HTMLElement) {
    shell.querySelector<HTMLInputElement>('input')?.focus()
    return
  }
  input?.focus()
}
</script>

<template>
  <section class="view-panel activity-view" aria-labelledby="activity-heading">
    <NotificationSectionHeader id="activity-heading" kicker="DELIVERY LEDGER" title="事件与投递">
      <template #actions
        ><a-button @click="emit('load')" :loading="activityLoading"
          ><template #icon><RefreshCw :size="15" /></template>刷新队列</a-button
        ></template
      >
    </NotificationSectionHeader>
    <a-tabs :active-key="activityView" class="activity-switcher" @change="changeActivityView">
      <a-tab-pane key="events"
        ><template #tab
          ><span class="activity-tab-label"
            >事件<a-badge :count="events.total" show-zero color="var(--color-info)" /></span></template
      ></a-tab-pane>
      <a-tab-pane key="deliveries"
        ><template #tab
          ><span class="activity-tab-label"
            >投递<a-badge
              :count="deliveries.total"
              show-zero
              :color="deadDeliveryCount > 0 ? 'var(--color-danger)' : 'var(--color-info)'" /></span></template
      ></a-tab-pane>
    </a-tabs>

    <section v-if="activityView === 'events'" class="activity-table data-card">
      <div class="filter-row event-filter-row">
        <div class="filter-search-shell" @click="focusSearchInput(eventSearchInput, $event)">
          <a-input
            ref="eventSearchInput"
            v-model:value="eventFilters.q"
            allow-clear
            class="filter-search"
            placeholder="搜索事件标题、正文、类型或来源"
            @press-enter="emit('apply-filters')"
            ><template #prefix><Search :size="14" /></template
          ></a-input>
        </div>
        <a-select
          v-model:value="eventFilters.severity"
          :options="severityOptions"
          style="width: 130px"
          @change="emit('apply-filters')"
        /><a-select
          v-model:value="eventFilters.routing_status"
          allow-clear
          placeholder="路由状态"
          style="width: 130px"
          @change="emit('apply-filters')"
          :options="[
            { value: 'routed', label: '已路由' },
            { value: 'unrouted', label: '未匹配' },
            { value: 'suppressed', label: '已抑制' },
            { value: 'targeted', label: '定向测试' },
          ]"
        /><a-button type="text" class="more-filter-button" @click="eventFiltersExpanded = !eventFiltersExpanded">
          {{ eventFiltersExpanded ? '收起更多' : '更多筛选' }} </a-button
        ><a-button aria-label="应用事件筛选" @click="emit('apply-filters')"
          ><template #icon><SlidersHorizontal :size="14" /></template>筛选</a-button
        >
      </div>
      <div v-if="eventFiltersExpanded" class="filter-row advanced-filter-row">
        <a-input
          v-model:value="eventFilters.event_type"
          allow-clear
          class="filter-field"
          placeholder="事件类型"
          @press-enter="emit('apply-filters')"
        />
        <a-input
          v-model:value="eventFilters.source"
          allow-clear
          class="filter-field"
          placeholder="来源"
          @press-enter="emit('apply-filters')"
        />
      </div>
      <a-table
        :data-source="events.items"
        :pagination="false"
        :scroll="{ x: 700 }"
        row-key="id"
        size="small"
        :loading="eventsLoading"
        ><a-table-column key="event" title="事件" :width="260"
          ><template #default="{ record }"
            ><div class="primary-cell">
              <strong>{{ record.title }}</strong
              ><small>{{ record.event_type }} · {{ record.source }}</small
              ><small class="event-body">{{ record.body }}</small>
            </div></template
          ></a-table-column
        ><a-table-column key="severity" title="级别" :width="88"
          ><template #default="{ record }"
            ><a-badge
              :status="statusBadge(record.severity)"
              :text="severityLabel(record.severity)" /></template></a-table-column
        ><a-table-column key="route" title="路由" :width="100"
          ><template #default="{ record }"
            ><a-badge
              :status="statusBadge(record.routing_status)"
              :text="statusLabel(record.routing_status)" /></template></a-table-column
        ><a-table-column key="occurred" title="发生时间" :width="150"
          ><template #default="{ record }"
            ><time class="mono-value">{{ formatFullTime(record.occurred_at) }}</time></template
          ></a-table-column
        ><a-table-column key="payload" title="详情" :width="78"
          ><template #default="{ record }"
            ><a-button
              type="text"
              size="small"
              class="detail-trigger"
              :aria-label="`查看事件 ${record.id} 详情`"
              @click="expandedEventId = expandedEventId === record.id ? null : record.id"
              ><template #icon><ChevronDown :size="14" :class="{ rotated: expandedEventId === record.id }" /></template
              >查看</a-button
            ></template
          ></a-table-column
        ></a-table
      >
      <div v-if="expandedEvent" class="event-detail" role="region" aria-label="事件详情">
        <div class="event-detail-heading">
          <strong>{{ expandedEvent.title }}</strong
          ><span class="mono-value">{{ expandedEvent.event_type }} · {{ expandedEvent.source }}</span>
        </div>
        <p>{{ expandedEvent.body || '无正文' }}</p>
        <div class="event-detail-meta">
          <span><b>事件 ID</b>{{ expandedEvent.id }}</span>
          <span><b>关联 ID</b>{{ expandedEvent.correlation_id || '—' }}</span>
          <span><b>匹配策略</b>{{ expandedEvent.matched_policy_id || '—' }}</span>
          <span><b>幂等键</b>{{ expandedEvent.idempotency_key }}</span>
          <span><b>指纹</b>{{ expandedEvent.fingerprint || '—' }}</span>
          <span><b>创建时间</b>{{ formatFullTime(expandedEvent.created_at) }}</span>
        </div>
        <pre>{{ JSON.stringify(expandedEvent.payload, null, 2) }}</pre>
      </div>
      <div class="table-footer">
        <span>共 {{ events.total }} 条事件</span
        ><a-pagination
          size="small"
          :current="Math.floor(events.offset / events.limit) + 1"
          :page-size="events.limit"
          :total="events.total"
          :show-size-changer="false"
          @change="(page: number) => emit('event-page', page)"
        />
      </div>
    </section>

    <section v-else class="activity-table data-card">
      <div class="filter-row delivery-filter-row">
        <div class="filter-search-shell" @click="focusSearchInput(deliverySearchInput, $event)">
          <a-input
            ref="deliverySearchInput"
            v-model:value="deliveryFilters.q"
            allow-clear
            class="filter-search"
            placeholder="搜索事件、端点或错误信息"
            @press-enter="emit('apply-filters')"
            ><template #prefix><Search :size="14" /></template
          ></a-input>
        </div>
        <a-select
          v-model:value="deliveryFilters.status"
          allow-clear
          placeholder="投递状态"
          style="width: 130px"
          :options="[
            { value: 'pending', label: '待发送' },
            { value: 'sending', label: '发送中' },
            { value: 'retry', label: '待重试' },
            { value: 'sent', label: '已发送' },
            { value: 'dead', label: '死信' },
          ]"
          @change="emit('apply-filters')"
        /><a-select
          v-model:value="deliveryFilters.endpoint_id"
          allow-clear
          show-search
          :filter-option="
            (input: string, option: { label?: string }) =>
              (option.label ?? '').toLowerCase().includes(input.toLowerCase())
          "
          placeholder="选择端点"
          class="endpoint-filter"
          :options="endpointOptions"
          @change="emit('apply-filters')"
        /><a-button type="text" class="more-filter-button" @click="deliveryFiltersExpanded = !deliveryFiltersExpanded">
          {{ deliveryFiltersExpanded ? '收起更多' : '更多筛选' }} </a-button
        ><a-button aria-label="应用投递筛选" @click="emit('apply-filters')"
          ><template #icon><SlidersHorizontal :size="14" /></template>筛选</a-button
        >
      </div>
      <div v-if="deliveryFiltersExpanded" class="filter-row advanced-filter-row">
        <a-input
          v-model:value="deliveryFilters.event_id"
          allow-clear
          class="filter-field"
          placeholder="事件 ID"
          @press-enter="emit('apply-filters')"
        />
      </div>
      <a-table
        :data-source="deliveries.items"
        :pagination="false"
        :scroll="{ x: 700 }"
        row-key="id"
        size="small"
        :loading="deliveriesLoading"
        ><a-table-column key="delivery" title="投递"
          ><template #default="{ record }"
            ><div class="primary-cell">
              <strong>{{ endpointById.get(record.endpoint_id)?.name ?? record.endpoint_id.slice(0, 12) + '…' }}</strong
              ><small>{{
                connectorById.get(endpointById.get(record.endpoint_id)?.connector_id ?? '')?.name ?? '快照连接器'
              }}</small>
            </div></template
          ></a-table-column
        ><a-table-column key="status" title="状态" :width="100"
          ><template #default="{ record }"
            ><a-badge
              :status="statusBadge(record.status)"
              :text="statusLabel(record.status)" /></template></a-table-column
        ><a-table-column key="attempts" title="尝试" :width="74"
          ><template #default="{ record }"
            ><span class="mono-value">{{ record.attempt_count }}</span></template
          ></a-table-column
        ><a-table-column key="updated" title="更新时间" :width="150"
          ><template #default="{ record }"
            ><time class="mono-value">{{ formatFullTime(record.updated_at) }}</time></template
          ></a-table-column
        ><a-table-column key="actions" title="操作" :width="86"
          ><template #default="{ record }"
            ><a-button
              v-if="record.status === 'dead' || record.status === 'retry'"
              type="link"
              size="small"
              @click="emit('retry-delivery', record)"
              ><template #icon><RotateCcw :size="14" /></template>重试</a-button
            ><span v-else class="muted-dash">—</span></template
          ></a-table-column
        ></a-table
      >
      <div class="table-footer">
        <span>共 {{ deliveries.total }} 条投递</span
        ><a-pagination
          size="small"
          :current="Math.floor(deliveries.offset / deliveries.limit) + 1"
          :page-size="deliveries.limit"
          :total="deliveries.total"
          :show-size-changer="false"
          @change="(page: number) => emit('delivery-page', page)"
        />
      </div>
    </section>
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
.activity-switcher :deep(.ant-tabs-nav) {
  margin: 0 0 10px;
}
.activity-switcher :deep(.ant-tabs-content-holder) {
  display: none;
}
.activity-tab-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}
.activity-table {
  overflow: hidden;
}
.mono-value {
  color: var(--muted);
  font: var(--type-meta) var(--font-family-mono);
}
.filter-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 11px 12px;
  border-bottom: 1px solid var(--line);
}
.filter-row :deep(.ant-input-affix-wrapper),
.filter-row :deep(.ant-input) {
  box-sizing: border-box;
}
.filter-row :deep(.ant-input-affix-wrapper) {
  width: min(290px, 100%);
}
.filter-search-shell {
  width: min(290px, 100%);
}
.filter-search-shell :deep(.ant-input-affix-wrapper) {
  width: 100%;
}
.filter-row :deep(.filter-field.ant-input) {
  width: 155px;
}
.filter-row :deep(.ant-input-affix-wrapper.filter-field) {
  width: 155px;
}
.filter-row :deep(.ant-input-affix-wrapper .ant-input) {
  width: 100%;
}
.filter-row :deep(.ant-select) {
  min-width: 125px;
}
.table-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 51px;
  padding: 8px 13px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--type-meta);
}
.muted-dash {
  color: var(--muted);
}
.event-body {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.advanced-filter-row {
  padding-top: 0;
}
.more-filter-button {
  color: var(--muted);
}
.detail-trigger {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.detail-trigger :deep(.rotated) {
  transform: rotate(180deg);
}
.event-detail {
  margin: 0 12px 12px;
  padding: 11px 13px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface-hover);
}
.event-detail-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.event-detail-heading > * {
  min-width: 0;
  overflow-wrap: anywhere;
}
.event-detail-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 16px;
  margin-bottom: 9px;
  color: var(--muted);
  font: var(--type-meta) var(--font-family-mono);
  overflow-wrap: anywhere;
}
.event-detail-meta b {
  margin-right: 7px;
  color: var(--text);
  font-family: var(--font-family-sans);
  font-weight: 500;
}
.event-detail p {
  margin: 8px 0;
  color: var(--muted);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.event-detail pre {
  max-height: 220px;
  margin: 0;
  overflow: auto;
  color: var(--text);
  font: var(--type-meta) var(--font-family-mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 600px) {
  .filter-row > :deep(.ant-input),
  .filter-row > :deep(.ant-input-affix-wrapper),
  .filter-row > :deep(.ant-select),
  .filter-row > :deep(.ant-btn) {
    width: 100% !important;
  }
  .filter-search-shell {
    width: 100%;
  }
  .table-footer {
    align-items: flex-start;
    flex-direction: column;
  }
  .event-detail-meta {
    grid-template-columns: 1fr;
  }
  .event-detail-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
