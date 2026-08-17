<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  Activity,
  AlertTriangle,
  Bell,
  CircleDot,
  Globe2,
  MessageCircle,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  SlidersHorizontal,
  Trash2,
  UsersRound
} from 'lucide-vue-next'
import { notificationApi } from '@/api/client'
import type {
  JsonObject,
  NotificationConnector,
  NotificationConnectorInput,
  NotificationDelivery,
  NotificationEndpoint,
  NotificationEndpointInput,
  NotificationEvent,
  NotificationGroup,
  NotificationGroupInput,
  NotificationOverview,
  NotificationPolicy,
  NotificationPolicyInput,
  NotificationSeverity,
  Page
} from '@/api/types'

type ViewKey = 'overview' | 'connectors' | 'groups' | 'policies' | 'activity'
type ActivityKey = 'events' | 'deliveries'

const view = ref<ViewKey>('overview')
const activityView = ref<ActivityKey>('events')
const loading = ref(false)
const activityLoading = ref(false)
const saving = ref(false)
const lastSyncedAt = ref<Date | null>(null)
const loadError = ref('')

const connectors = ref<NotificationConnector[]>([])
const endpoints = ref<NotificationEndpoint[]>([])
const groups = ref<NotificationGroup[]>([])
const policies = ref<NotificationPolicy[]>([])
const overview = ref<NotificationOverview>({
  connectors: 0,
  enabled_connectors: 0,
  endpoints: 0,
  enabled_endpoints: 0,
  groups: 0,
  policies: 0,
  events: 0,
  unrouted_events: 0,
  deliveries: { pending: 0, sending: 0, retry: 0, sent: 0, dead: 0 }
})
const events = ref<Page<NotificationEvent>>({ items: [], total: 0, limit: 8, offset: 0 })
const deliveries = ref<Page<NotificationDelivery>>({ items: [], total: 0, limit: 8, offset: 0 })

const eventFilters = reactive({ event_type: '', severity: '', routing_status: '' })
const deliveryFilters = reactive({ status: '', endpoint_id: '', event_id: '' })

const connectorModalOpen = ref(false)
const endpointModalOpen = ref(false)
const groupModalOpen = ref(false)
const policyModalOpen = ref(false)
const connectorEditingId = ref<string | null>(null)
const endpointEditingId = ref<string | null>(null)
const groupEditingId = ref<string | null>(null)
const policyEditingId = ref<string | null>(null)

const connectorForm = reactive({
  name: '',
  type: 'telegram' as 'telegram' | 'webhook',
  secret_ref: '',
  parse_mode: 'HTML',
  timeout_seconds: 8,
  auth_type: 'hmac_sha256' as 'none' | 'bearer' | 'hmac_sha256',
  allow_http: false,
  enabled: true,
  version: 1
})
const endpointForm = reactive({
  connector_id: '',
  name: '',
  address: '',
  topic_id: '',
  headers_json: '{}',
  enabled: true,
  version: 1
})
const groupForm = reactive({
  name: '',
  description: '',
  endpoint_ids: [] as string[],
  enabled: true,
  version: 1
})
const policyForm = reactive({
  name: '',
  event_pattern: '',
  severity: 'warning' as NotificationSeverity,
  priority: 0,
  suppress: false,
  group_ids: [] as string[],
  enabled: true,
  version: 1
})

const viewOptions: Array<{ key: ViewKey; label: string; icon: typeof Bell }> = [
  { key: 'overview', label: '概览', icon: Activity },
  { key: 'connectors', label: '连接器与端点', icon: Settings2 },
  { key: 'groups', label: '职责组', icon: UsersRound },
  { key: 'policies', label: '路由策略', icon: SlidersHorizontal },
  { key: 'activity', label: '事件与投递', icon: Bell }
]

const severityOptions = [
  { value: '', label: '全部级别' },
  { value: 'info', label: '信息' },
  { value: 'warning', label: '预警' },
  { value: 'critical', label: '严重' }
]

const connectorById = computed(() => new Map(connectors.value.map((item) => [item.id, item])))
const endpointById = computed(() => new Map(endpoints.value.map((item) => [item.id, item])))
const groupById = computed(() => new Map(groups.value.map((item) => [item.id, item])))
const enabledEndpointCount = computed(() => endpoints.value.filter((item) => item.enabled).length)
const deadDeliveryCount = computed(() => overview.value.deliveries.dead ?? 0)
const retryDeliveryCount = computed(() => (overview.value.deliveries.retry ?? 0) + (overview.value.deliveries.pending ?? 0))
const selectedConnector = computed(() => connectorById.value.get(endpointForm.connector_id) ?? null)
const activeTitle = computed(() => viewOptions.find((item) => item.key === view.value)?.label ?? '通知中心')

function normalizeOverview(value: unknown): NotificationOverview {
  const source = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const deliverySource = (source.deliveries ?? source.delivery_statuses ?? {}) as Record<string, unknown>
  return {
    connectors: Number(source.connectors ?? 0),
    enabled_connectors: Number(source.enabled_connectors ?? 0),
    endpoints: Number(source.endpoints ?? 0),
    enabled_endpoints: Number(source.enabled_endpoints ?? 0),
    groups: Number(source.groups ?? 0),
    policies: Number(source.policies ?? 0),
    events: Number(source.events ?? 0),
    unrouted_events: Number(source.unrouted_events ?? 0),
    deliveries: {
      pending: Number(deliverySource.pending ?? 0),
      sending: Number(deliverySource.sending ?? 0),
      retry: Number(deliverySource.retry ?? 0),
      sent: Number(deliverySource.sent ?? 0),
      dead: Number(deliverySource.dead ?? 0)
    }
  }
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback
}

async function loadEvents() {
  activityLoading.value = true
  try {
    events.value = await notificationApi.events({
      limit: events.value.limit,
      offset: events.value.offset,
      ...eventFilters
    })
  } catch (error) {
    loadError.value = errorMessage(error, '通知事件加载失败')
  } finally {
    activityLoading.value = false
  }
}

async function loadDeliveries() {
  activityLoading.value = true
  try {
    deliveries.value = await notificationApi.deliveries({
      limit: deliveries.value.limit,
      offset: deliveries.value.offset,
      ...deliveryFilters
    })
  } catch (error) {
    loadError.value = errorMessage(error, '通知投递加载失败')
  } finally {
    activityLoading.value = false
  }
}

async function loadActivity() {
  if (activityView.value === 'events') await loadEvents()
  else await loadDeliveries()
}

async function refreshOverview() {
  try {
    overview.value = normalizeOverview(await notificationApi.overview())
  } catch (error) {
    message.warning(errorMessage(error, '通知概览刷新失败'))
  }
}

async function loadAll() {
  loading.value = true
  loadError.value = ''
  const results = await Promise.allSettled([
    notificationApi.overview(),
    notificationApi.connectors(),
    notificationApi.endpoints(),
    notificationApi.groups(),
    notificationApi.policies(),
    notificationApi.events({ limit: events.value.limit, offset: 0 }),
    notificationApi.deliveries({ limit: deliveries.value.limit, offset: 0 })
  ])
  const [overviewResult, connectorsResult, endpointsResult, groupsResult, policiesResult, eventsResult, deliveriesResult] = results
  if (overviewResult.status === 'fulfilled') overview.value = normalizeOverview(overviewResult.value)
  if (connectorsResult.status === 'fulfilled') connectors.value = connectorsResult.value.items
  if (endpointsResult.status === 'fulfilled') endpoints.value = endpointsResult.value.items
  if (groupsResult.status === 'fulfilled') groups.value = groupsResult.value.items
  if (policiesResult.status === 'fulfilled') policies.value = policiesResult.value.items
  if (eventsResult.status === 'fulfilled') events.value = eventsResult.value
  if (deliveriesResult.status === 'fulfilled') deliveries.value = deliveriesResult.value
  if (results.some((item) => item.status === 'rejected')) loadError.value = '部分通知数据暂时不可用，请刷新重试。'
  lastSyncedAt.value = new Date()
  loading.value = false
}

function formatDate(value: string | null | undefined) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date)
}

function formatLongDate(value: string | null | undefined) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date)
}

function connectorLabel(type: string) {
  return type === 'telegram' ? 'Telegram Bot' : 'Webhook'
}

function severityLabel(value: string) {
  return ({ info: '信息', warning: '预警', critical: '严重' } as Record<string, string>)[value] ?? value
}

function statusLabel(value: string) {
  return ({ pending: '待发送', sending: '发送中', retry: '待重试', sent: '已发送', dead: '死信', routed: '已路由', suppressed: '已抑制', unrouted: '未匹配', targeted: '定向测试' } as Record<string, string>)[value] ?? value
}

function statusClass(value: string) {
  return `status-${value}`
}

function endpointNames(ids: string[]) {
  return ids.map((id) => endpointById.value.get(id)?.name ?? `${id.slice(0, 8)}…`).join('、') || '未配置端点'
}

function groupNames(ids: string[]) {
  return ids.map((id) => groupById.value.get(id)?.name ?? `${id.slice(0, 8)}…`).join('、') || '未选择职责组'
}

function connectorConfigSummary(connector: NotificationConnector) {
  if (connector.type === 'telegram') return connector.secret_ref ? `密钥：${connector.secret_ref}` : '未绑定密钥引用'
  const timeout = connector.config?.timeout_seconds
  return timeout ? `超时 ${timeout}s` : '标准 HTTP webhook'
}

function endpointConfigSummary(endpoint: NotificationEndpoint) {
  const connector = connectorById.value.get(endpoint.connector_id)
  if (connector?.type === 'telegram') {
    const topic = endpoint.config?.message_thread_id ?? endpoint.config?.topic_id
    return topic ? `chat ${endpoint.address} · Topic ${topic}` : `chat ${endpoint.address}`
  }
  return endpoint.address
}

function validateEndpointAddress(connector: NotificationConnector, address: string) {
  if (connector.type === 'telegram') return true
  try {
    const parsed = new URL(address)
    const allowHttp = Boolean(connector.config?.allow_http)
    if (parsed.protocol !== 'https:' && !(allowHttp && parsed.protocol === 'http:')) {
      throw new Error('Webhook 默认必须使用 HTTPS；仅允许已开启内网调试时使用 HTTP')
    }
    if (parsed.username || parsed.password) throw new Error('Webhook URL 不应包含账号或密码')
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('Webhook')) throw error
    throw new Error('请输入合法的 Webhook URL')
  }
  return true
}

function parseJsonObject(value: string, fallback: JsonObject = {}) {
  if (!value.trim()) return fallback
  try {
    const parsed: unknown = JSON.parse(value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('必须是 JSON 对象')
    return parsed as JsonObject
  } catch {
    throw new Error('请求头必须是合法的 JSON 对象')
  }
}

function resetConnectorForm(item?: NotificationConnector) {
  connectorEditingId.value = item?.id ?? null
  Object.assign(connectorForm, {
    name: item?.name ?? '',
    type: item?.type ?? 'telegram',
    secret_ref: item?.secret_ref ?? '',
    parse_mode: String(item?.config?.parse_mode ?? 'HTML'),
    timeout_seconds: Number(item?.config?.timeout_seconds ?? 8),
    auth_type: String(item?.config?.auth_type ?? 'hmac_sha256') as 'none' | 'bearer' | 'hmac_sha256',
    allow_http: Boolean(item?.config?.allow_http ?? false),
    enabled: item?.enabled ?? true,
    version: item?.version ?? 1
  })
  connectorModalOpen.value = true
}

function resetEndpointForm(item?: NotificationEndpoint, connectorId?: string) {
  endpointEditingId.value = item?.id ?? null
  Object.assign(endpointForm, {
    connector_id: item?.connector_id ?? connectorId ?? connectors.value[0]?.id ?? '',
    name: item?.name ?? '',
    address: item?.address ?? '',
    topic_id: String(item?.config?.message_thread_id ?? item?.config?.topic_id ?? ''),
    headers_json: JSON.stringify(item?.config?.headers ?? {}, null, 2),
    enabled: item?.enabled ?? true,
    version: item?.version ?? 1
  })
  endpointModalOpen.value = true
}

function resetGroupForm(item?: NotificationGroup) {
  groupEditingId.value = item?.id ?? null
  Object.assign(groupForm, {
    name: item?.name ?? '',
    description: item?.description ?? '',
    endpoint_ids: [...(item?.endpoint_ids ?? [])],
    enabled: item?.enabled ?? true,
    version: item?.version ?? 1
  })
  groupModalOpen.value = true
}

function resetPolicyForm(item?: NotificationPolicy) {
  policyEditingId.value = item?.id ?? null
  Object.assign(policyForm, {
    name: item?.name ?? '',
    event_pattern: item?.event_pattern ?? '',
    severity: item?.severity ?? 'warning',
    priority: item?.priority ?? 0,
    suppress: item?.suppress ?? false,
    group_ids: [...(item?.group_ids ?? [])],
    enabled: item?.enabled ?? true,
    version: item?.version ?? 1
  })
  policyModalOpen.value = true
}

async function submitConnector() {
  if (!connectorForm.name.trim()) return message.error('请输入连接器名称')
  if ((connectorForm.type === 'telegram' || connectorForm.auth_type !== 'none') && !connectorForm.secret_ref.trim()) return message.error('当前认证模式需要密钥引用')
  saving.value = true
  const body: NotificationConnectorInput = {
    name: connectorForm.name.trim(),
    type: connectorForm.type,
    secret_ref: connectorForm.secret_ref.trim(),
    enabled: connectorForm.enabled,
    config: connectorForm.type === 'telegram'
      ? { parse_mode: connectorForm.parse_mode || 'HTML' }
      : {
        timeout_seconds: Number(connectorForm.timeout_seconds) || 8,
        auth_type: connectorForm.auth_type,
        allow_http: connectorForm.allow_http
      }
  }
  try {
    const result = connectorEditingId.value
      ? await notificationApi.updateConnector(connectorEditingId.value, { ...body, expected_version: connectorForm.version })
      : await notificationApi.createConnector(body)
    if (connectorEditingId.value) {
      connectors.value = connectors.value.map((item) => item.id === connectorEditingId.value ? result : item)
    } else connectors.value = [...connectors.value, result]
    connectorModalOpen.value = false
    message.success(connectorEditingId.value ? '连接器已更新' : '连接器已创建')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '连接器保存失败'))
  } finally {
    saving.value = false
  }
}

async function submitEndpoint() {
  if (!endpointForm.connector_id) return message.error('请选择连接器')
  if (!endpointForm.name.trim() || !endpointForm.address.trim()) return message.error('请填写端点名称和地址')
  const connector = connectorById.value.get(endpointForm.connector_id)
  if (!connector) return message.error('连接器不存在，请刷新后重试')
  try {
    validateEndpointAddress(connector, endpointForm.address.trim())
  } catch (error) {
    return message.error(errorMessage(error, '端点地址格式错误'))
  }
  let config: JsonObject = {}
  try {
    if (connector.type === 'telegram') {
      if (endpointForm.topic_id.trim()) config.message_thread_id = Number(endpointForm.topic_id) || endpointForm.topic_id.trim()
    } else config = { headers: parseJsonObject(endpointForm.headers_json) }
  } catch (error) {
    return message.error(errorMessage(error, '端点配置格式错误'))
  }
  saving.value = true
  const body: NotificationEndpointInput = {
    connector_id: endpointForm.connector_id,
    name: endpointForm.name.trim(),
    address: endpointForm.address.trim(),
    config,
    enabled: endpointForm.enabled
  }
  try {
    const result = endpointEditingId.value
      ? await notificationApi.updateEndpoint(endpointEditingId.value, { ...body, expected_version: endpointForm.version })
      : await notificationApi.createEndpoint(body)
    if (endpointEditingId.value) endpoints.value = endpoints.value.map((item) => item.id === endpointEditingId.value ? result : item)
    else endpoints.value = [...endpoints.value, result]
    endpointModalOpen.value = false
    message.success(endpointEditingId.value ? '端点已更新' : '端点已创建')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '端点保存失败'))
  } finally {
    saving.value = false
  }
}

async function submitGroup() {
  if (!groupForm.name.trim()) return message.error('请输入职责组名称')
  if (!groupForm.endpoint_ids.length) return message.error('至少选择一个通知端点')
  saving.value = true
  const body: NotificationGroupInput = {
    name: groupForm.name.trim(),
    description: groupForm.description.trim() || null,
    endpoint_ids: [...groupForm.endpoint_ids],
    enabled: groupForm.enabled
  }
  try {
    const result = groupEditingId.value
      ? await notificationApi.updateGroup(groupEditingId.value, { ...body, expected_version: groupForm.version })
      : await notificationApi.createGroup(body)
    if (groupEditingId.value) groups.value = groups.value.map((item) => item.id === groupEditingId.value ? result : item)
    else groups.value = [...groups.value, result]
    groupModalOpen.value = false
    message.success(groupEditingId.value ? '职责组已更新' : '职责组已创建')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '职责组保存失败'))
  } finally {
    saving.value = false
  }
}

async function submitPolicy() {
  if (!policyForm.name.trim() || !policyForm.event_pattern.trim()) return message.error('请填写策略名称和事件模式')
  if (!policyForm.suppress && !policyForm.group_ids.length) return message.error('非抑制策略至少选择一个职责组')
  saving.value = true
  const body: NotificationPolicyInput = {
    name: policyForm.name.trim(),
    event_pattern: policyForm.event_pattern.trim(),
    severity: policyForm.severity,
    priority: Number(policyForm.priority) || 0,
    suppress: policyForm.suppress,
    group_ids: [...policyForm.group_ids],
    enabled: policyForm.enabled
  }
  try {
    const result = policyEditingId.value
      ? await notificationApi.updatePolicy(policyEditingId.value, { ...body, expected_version: policyForm.version })
      : await notificationApi.createPolicy(body)
    if (policyEditingId.value) policies.value = policies.value.map((item) => item.id === policyEditingId.value ? result : item)
    else policies.value = [...policies.value, result]
    policyModalOpen.value = false
    message.success(policyEditingId.value ? '策略已更新' : '策略已创建')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '策略保存失败'))
  } finally {
    saving.value = false
  }
}

async function toggleConnector(item: NotificationConnector, enabled: boolean) {
  const previous = item.enabled
  item.enabled = enabled
  try {
    const updated = await notificationApi.updateConnector(item.id, {
      name: item.name, type: item.type, secret_ref: item.secret_ref,
      config: item.config, enabled, expected_version: item.version
    })
    connectors.value = connectors.value.map((row) => row.id === item.id ? updated : row)
    await refreshOverview()
  } catch (error) {
    item.enabled = previous
    message.error(errorMessage(error, '连接器状态更新失败'))
  }
}

async function toggleEndpoint(item: NotificationEndpoint, enabled: boolean) {
  const previous = item.enabled
  item.enabled = enabled
  try {
    const updated = await notificationApi.updateEndpoint(item.id, {
      connector_id: item.connector_id, name: item.name, address: item.address,
      config: item.config, enabled, expected_version: item.version
    })
    endpoints.value = endpoints.value.map((row) => row.id === item.id ? updated : row)
    await refreshOverview()
  } catch (error) {
    item.enabled = previous
    message.error(errorMessage(error, '端点状态更新失败'))
  }
}

async function toggleGroup(item: NotificationGroup, enabled: boolean) {
  const previous = item.enabled
  item.enabled = enabled
  try {
    const updated = await notificationApi.updateGroup(item.id, {
      name: item.name, description: item.description, endpoint_ids: item.endpoint_ids,
      enabled, expected_version: item.version
    })
    groups.value = groups.value.map((row) => row.id === item.id ? updated : row)
    await refreshOverview()
  } catch (error) {
    item.enabled = previous
    message.error(errorMessage(error, '职责组状态更新失败'))
  }
}

async function togglePolicy(item: NotificationPolicy, enabled: boolean) {
  const previous = item.enabled
  item.enabled = enabled
  try {
    const updated = await notificationApi.updatePolicy(item.id, {
      name: item.name, event_pattern: item.event_pattern, severity: item.severity,
      priority: item.priority, suppress: item.suppress, group_ids: item.group_ids,
      enabled, expected_version: item.version
    })
    policies.value = policies.value.map((row) => row.id === item.id ? updated : row)
    await refreshOverview()
  } catch (error) {
    item.enabled = previous
    message.error(errorMessage(error, '策略状态更新失败'))
  }
}

async function deleteConnector(item: NotificationConnector) {
  try {
    await notificationApi.deleteConnector(item.id, item.version)
    connectors.value = connectors.value.filter((row) => row.id !== item.id)
    endpoints.value = endpoints.value.filter((row) => row.connector_id !== item.id)
    message.success('连接器已删除')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '连接器删除失败；请确认没有关联端点'))
  }
}

async function deleteEndpoint(item: NotificationEndpoint) {
  try {
    await notificationApi.deleteEndpoint(item.id, item.version)
    endpoints.value = endpoints.value.filter((row) => row.id !== item.id)
    groups.value = groups.value.map((row) => ({ ...row, endpoint_ids: row.endpoint_ids.filter((id) => id !== item.id) }))
    message.success('端点已删除')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '端点删除失败；请先移出职责组'))
  }
}

async function deleteGroup(item: NotificationGroup) {
  try {
    await notificationApi.deleteGroup(item.id, item.version)
    groups.value = groups.value.filter((row) => row.id !== item.id)
    policies.value = policies.value.map((row) => ({ ...row, group_ids: row.group_ids.filter((id) => id !== item.id) }))
    message.success('职责组已删除')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '职责组删除失败；请先移出路由策略'))
  }
}

async function deletePolicy(item: NotificationPolicy) {
  try {
    await notificationApi.deletePolicy(item.id, item.version)
    policies.value = policies.value.filter((row) => row.id !== item.id)
    message.success('路由策略已删除')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '路由策略删除失败'))
  }
}

async function testEndpoint(item: NotificationEndpoint) {
  try {
    const result = await notificationApi.testEndpoint(item.id)
    if (result.event) {
      const nextEvents = [result.event, ...events.value.items.filter((row) => row.id !== result.event.id)]
      events.value = { ...events.value, items: nextEvents, total: Math.max(events.value.total, nextEvents.length) }
    }
    if (result.deliveries?.length) {
      const incoming = new Map(result.deliveries.map((row) => [row.id, row]))
      const nextDeliveries = [
        ...result.deliveries,
        ...deliveries.value.items.filter((row) => !incoming.has(row.id))
      ]
      deliveries.value = { ...deliveries.value, items: nextDeliveries, total: Math.max(deliveries.value.total, nextDeliveries.length) }
    }
    message.success(`测试通知已进入队列：${item.name}`)
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '测试通知发送失败'))
  }
}

async function retryDelivery(item: NotificationDelivery) {
  try {
    const updated = await notificationApi.retryDelivery(item.id)
    deliveries.value = {
      ...deliveries.value,
      items: deliveries.value.items.map((row) => row.id === updated.id ? updated : row)
    }
    message.success('投递已重新排队')
    await refreshOverview()
  } catch (error) {
    message.error(errorMessage(error, '投递重试失败'))
  }
}

function changeEventPage(page: number) {
  events.value.offset = (page - 1) * events.value.limit
  void loadEvents()
}

function changeDeliveryPage(page: number) {
  deliveries.value.offset = (page - 1) * deliveries.value.limit
  void loadDeliveries()
}

watch([view, activityView], ([nextView]) => {
  if (nextView === 'activity') void loadActivity()
})

onMounted(() => { void loadAll() })
</script>

<template>
  <main class="notification-page">
    <header class="notification-heading">
      <div class="heading-copy">
        <span class="eyebrow">SYSTEM / NOTIFICATIONS</span>
        <div class="title-line">
          <span class="title-icon"><Bell :size="19" /></span>
          <div>
            <h1>通知中心</h1>
            <p>统一管理故障、风控和交易信号的多渠道投递。</p>
          </div>
        </div>
      </div>
      <div class="heading-actions">
        <span v-if="lastSyncedAt" class="sync-stamp">同步于 {{ formatDate(lastSyncedAt.toISOString()) }}</span>
        <a-button class="icon-button" :loading="loading" aria-label="刷新通知数据" @click="loadAll">
          <template #icon><RefreshCw :size="15" /></template>
          刷新
        </a-button>
      </div>
    </header>

    <nav class="view-switcher" aria-label="通知中心视图">
      <button
        v-for="item in viewOptions"
        :key="item.key"
        type="button"
        class="view-tab"
        :class="{ active: view === item.key }"
        role="tab"
        :aria-selected="view === item.key"
        @click="view = item.key"
      >
        <component :is="item.icon" :size="15" />
        <span>{{ item.label }}</span>
        <b v-if="item.key === 'activity' && deadDeliveryCount" class="tab-count danger">{{ deadDeliveryCount }}</b>
      </button>
    </nav>

    <div v-if="loadError" class="inline-alert" role="alert">
      <AlertTriangle :size="16" />
      <span>{{ loadError }}</span>
      <a-button type="link" size="small" @click="loadAll">重试</a-button>
    </div>

    <div v-if="loading" class="query-state notification-loading">
      <a-spin size="small" />
      <span>正在读取通知配置…</span>
    </div>

    <template v-else>
      <section v-if="view === 'overview'" class="view-panel overview-view" aria-labelledby="overview-heading">
        <div class="section-heading">
          <div>
            <span class="section-kicker">CONTROL ROOM</span>
            <h2 id="overview-heading">{{ activeTitle }}</h2>
          </div>
          <span class="section-note">配置变更即时生效，历史投递保留原始快照</span>
        </div>

        <div class="metric-strip">
          <div class="metric-item">
            <span>启用连接器</span>
            <strong>{{ overview.enabled_connectors }}<small>/{{ overview.connectors }}</small></strong>
            <em>Telegram / Webhook</em>
          </div>
          <div class="metric-item">
            <span>活跃端点</span>
            <strong>{{ enabledEndpointCount }}<small>/{{ overview.endpoints }}</small></strong>
            <em>独立地址隔离</em>
          </div>
          <div class="metric-item">
            <span>路由策略</span>
            <strong>{{ overview.policies }}</strong>
            <em>{{ overview.groups }} 个职责组</em>
          </div>
          <div class="metric-item" :class="{ 'metric-warning': retryDeliveryCount > 0 }">
            <span>待处理投递</span>
            <strong>{{ retryDeliveryCount }}</strong>
            <em>{{ deadDeliveryCount ? `${deadDeliveryCount} 条死信` : '当前无死信' }}</em>
          </div>
        </div>

        <div class="overview-grid">
          <section class="surface-panel health-panel">
            <div class="panel-heading">
              <div><CircleDot :size="16" class="panel-icon" /><h3>投递状态</h3></div>
              <span class="quiet-value">近期开关</span>
            </div>
            <div class="health-list">
              <div><span><i class="dot dot-green" />已发送</span><strong>{{ overview.deliveries.sent }}</strong></div>
              <div><span><i class="dot dot-blue" />待发送</span><strong>{{ overview.deliveries.pending }}</strong></div>
              <div><span><i class="dot dot-amber" />重试中</span><strong>{{ overview.deliveries.retry }}</strong></div>
              <div><span><i class="dot dot-red" />死信</span><strong>{{ overview.deliveries.dead }}</strong></div>
            </div>
            <a-button type="link" class="panel-link" @click="view = 'activity'; activityView = 'deliveries'">查看投递队列 <span>→</span></a-button>
          </section>
          <section class="surface-panel quick-panel">
            <div class="panel-heading">
              <div><SlidersHorizontal :size="16" class="panel-icon" /><h3>快速配置</h3></div>
              <span class="quiet-value">常用动作</span>
            </div>
            <button type="button" class="quick-action" @click="resetConnectorForm()">
              <span class="quick-mark telegram"><MessageCircle :size="15" /></span>
              <span><strong>添加 Telegram Bot</strong><small>一个 Bot 可承载多个群组 / Topic</small></span>
              <span class="quick-arrow">＋</span>
            </button>
            <button type="button" class="quick-action" @click="resetConnectorForm(); connectorForm.type = 'webhook'">
              <span class="quick-mark webhook"><Globe2 :size="15" /></span>
              <span><strong>添加 Webhook</strong><small>同一鉴权配置可挂多个 URL</small></span>
              <span class="quick-arrow">＋</span>
            </button>
            <button type="button" class="quick-action" @click="resetPolicyForm()">
              <span class="quick-mark policy"><SlidersHorizontal :size="15" /></span>
              <span><strong>新建路由策略</strong><small>按事件模式和重要级别选择职责组</small></span>
              <span class="quick-arrow">＋</span>
            </button>
          </section>
        </div>

        <section class="surface-panel recent-panel">
          <div class="panel-heading">
            <div><Activity :size="16" class="panel-icon" /><h3>最近事件</h3></div>
            <a-button type="link" class="panel-link" @click="view = 'activity'; activityView = 'events'">全部事件 <span>→</span></a-button>
          </div>
          <div v-if="events.items.length" class="event-list">
            <div v-for="event in events.items.slice(0, 5)" :key="event.id" class="event-row">
              <span class="event-severity" :class="statusClass(event.severity)"><i /></span>
              <div class="event-copy"><strong>{{ event.title }}</strong><small>{{ event.event_type }} · {{ event.source }}</small></div>
              <span class="event-state" :class="statusClass(event.routing_status)">{{ statusLabel(event.routing_status) }}</span>
              <time>{{ formatDate(event.occurred_at) }}</time>
            </div>
          </div>
          <a-empty v-else description="暂无通知事件" />
        </section>
      </section>

      <section v-else-if="view === 'connectors'" class="view-panel" aria-labelledby="connectors-heading">
        <div class="section-heading">
          <div><span class="section-kicker">CHANNEL FABRIC</span><h2 id="connectors-heading">连接器与端点</h2></div>
          <div class="section-actions">
            <a-button @click="resetEndpointForm()"><template #icon><Plus :size="15" /></template>添加端点</a-button>
            <a-button type="primary" @click="resetConnectorForm()"><template #icon><Plus :size="15" /></template>添加连接器</a-button>
          </div>
        </div>
        <p class="section-description">连接器代表发送身份与公共鉴权；端点代表具体 Telegram chat / topic 或 Webhook URL。</p>
        <div class="connector-list">
          <section v-for="connector in connectors" :key="connector.id" class="connector-block surface-panel">
            <div class="connector-header">
              <div class="connector-identity">
                <span class="connector-mark" :class="connector.type"><MessageCircle v-if="connector.type === 'telegram'" :size="17" /><Globe2 v-else :size="17" /></span>
                <div><div class="connector-name"><strong>{{ connector.name }}</strong><a-tag :color="connector.type === 'telegram' ? 'blue' : 'cyan'">{{ connectorLabel(connector.type) }}</a-tag></div><small>{{ connectorConfigSummary(connector) }} · v{{ connector.version }}</small></div>
              </div>
              <div class="connector-actions"><a-switch :checked="connector.enabled" size="small" @change="(checked: boolean) => toggleConnector(connector, checked)" /><a-button type="text" aria-label="编辑连接器" @click="resetConnectorForm(connector)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此连接器？必须先移除所有端点。" ok-text="删除" cancel-text="取消" @confirm="deleteConnector(connector)"><a-button type="text" danger aria-label="删除连接器"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></div>
            </div>
            <div class="endpoint-head"><span>端点</span><div><span>{{ endpoints.filter((item) => item.connector_id === connector.id).length }} 个地址</span><a-button type="link" size="small" @click="resetEndpointForm(undefined, connector.id)"><template #icon><Plus :size="13" /></template>{{ connector.type === 'webhook' ? '添加 URL' : '添加 Chat / Topic' }}</a-button></div></div>
            <div v-if="endpoints.filter((item) => item.connector_id === connector.id).length" class="endpoint-list">
              <div v-for="endpoint in endpoints.filter((item) => item.connector_id === connector.id)" :key="endpoint.id" class="endpoint-row">
                <span class="endpoint-state" :class="{ enabled: endpoint.enabled }" />
                <div class="endpoint-copy"><strong>{{ endpoint.name }}</strong><small>{{ endpointConfigSummary(endpoint) }}</small></div>
                <span class="endpoint-version">v{{ endpoint.version }}</span>
                <a-switch :checked="endpoint.enabled" size="small" @change="(checked: boolean) => toggleEndpoint(endpoint, checked)" />
                <a-tooltip title="发送测试通知"><a-button type="text" aria-label="发送测试通知" @click="testEndpoint(endpoint)"><template #icon><Send :size="15" /></template></a-button></a-tooltip>
                <a-button type="text" aria-label="编辑端点" @click="resetEndpointForm(endpoint)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此通知端点？" ok-text="删除" cancel-text="取消" @confirm="deleteEndpoint(endpoint)"><a-button type="text" danger aria-label="删除端点"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm>
              </div>
            </div>
            <div v-else class="empty-inline"><span>该连接器还没有接收端点</span><a-button type="link" @click="resetEndpointForm(undefined, connector.id)">添加第一个端点</a-button></div>
          </section>
          <div v-if="!connectors.length" class="query-empty"><a-empty description="尚未配置连接器"><template #extra><a-button type="primary" @click="resetConnectorForm()"><template #icon><Plus :size="15" /></template>创建连接器</a-button></template></a-empty></div>
        </div>
      </section>

      <section v-else-if="view === 'groups'" class="view-panel" aria-labelledby="groups-heading">
        <div class="section-heading">
          <div><span class="section-kicker">RESPONSIBILITY MAP</span><h2 id="groups-heading">职责组</h2></div>
          <a-button type="primary" @click="resetGroupForm()"><template #icon><Plus :size="15" /></template>新建职责组</a-button>
        </div>
        <p class="section-description">把端点编成稳定的职责边界。策略只引用职责组，替换 Bot、群组或 URL 时无需修改策略。</p>
        <div class="data-table-wrap surface-panel">
          <a-table :data-source="groups" :pagination="false" row-key="id" size="small">
            <a-table-column key="name" title="职责组" :width="220">
              <template #default="{ record }"><div class="primary-cell"><strong>{{ record.name }}</strong><small>{{ record.description || '未填写说明' }}</small></div></template>
            </a-table-column>
            <a-table-column key="endpoints" title="成员端点">
              <template #default="{ record }"><span class="wrap-value">{{ endpointNames(record.endpoint_ids) }}</span></template>
            </a-table-column>
            <a-table-column key="version" title="版本" :width="80"><template #default="{ record }"><span class="mono-value">v{{ record.version }}</span></template></a-table-column>
            <a-table-column key="enabled" title="状态" :width="100"><template #default="{ record }"><a-switch :checked="record.enabled" size="small" @change="(checked: boolean) => toggleGroup(record, checked)" /></template></a-table-column>
            <a-table-column key="actions" title="操作" :width="116"><template #default="{ record }"><a-button type="text" aria-label="编辑职责组" @click="resetGroupForm(record)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此职责组？" ok-text="删除" cancel-text="取消" @confirm="deleteGroup(record)"><a-button type="text" danger aria-label="删除职责组"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></template></a-table-column>
          </a-table>
          <div v-if="!groups.length" class="table-empty"><a-empty description="暂无职责组" /></div>
        </div>
      </section>

      <section v-else-if="view === 'policies'" class="view-panel" aria-labelledby="policies-heading">
        <div class="section-heading">
          <div><span class="section-kicker">ROUTING LOGIC</span><h2 id="policies-heading">路由策略</h2></div>
          <a-button type="primary" @click="resetPolicyForm()"><template #icon><Plus :size="15" /></template>新建策略</a-button>
        </div>
        <p class="section-description">精确事件模式优先，其次按优先级选择；一个策略可通知一个或多个职责组，也可以显式抑制。</p>
        <div class="data-table-wrap surface-panel">
          <a-table :data-source="policies" :pagination="false" row-key="id" size="small">
            <a-table-column key="name" title="策略" :width="190"><template #default="{ record }"><div class="primary-cell"><strong>{{ record.name }}</strong><small>v{{ record.version }} · {{ record.suppress ? '显式抑制' : '正常路由' }}</small></div></template></a-table-column>
            <a-table-column key="pattern" title="事件模式"><template #default="{ record }"><code class="pattern-code">{{ record.event_pattern }}</code></template></a-table-column>
            <a-table-column key="severity" title="级别" :width="92"><template #default="{ record }"><span class="severity-badge" :class="statusClass(record.severity)"><i />{{ severityLabel(record.severity) }}</span></template></a-table-column>
            <a-table-column key="priority" title="优先级" :width="76"><template #default="{ record }"><span class="mono-value">{{ record.priority }}</span></template></a-table-column>
            <a-table-column key="groups" title="职责组"><template #default="{ record }"><span class="wrap-value">{{ record.suppress ? '—' : groupNames(record.group_ids) }}</span></template></a-table-column>
            <a-table-column key="enabled" title="状态" :width="100"><template #default="{ record }"><a-switch :checked="record.enabled" size="small" @change="(checked: boolean) => togglePolicy(record, checked)" /></template></a-table-column>
            <a-table-column key="actions" title="操作" :width="116"><template #default="{ record }"><a-button type="text" aria-label="编辑路由策略" @click="resetPolicyForm(record)"><template #icon><Pencil :size="15" /></template></a-button><a-popconfirm title="确认删除此路由策略？" ok-text="删除" cancel-text="取消" @confirm="deletePolicy(record)"><a-button type="text" danger aria-label="删除路由策略"><template #icon><Trash2 :size="15" /></template></a-button></a-popconfirm></template></a-table-column>
          </a-table>
          <div v-if="!policies.length" class="table-empty"><a-empty description="暂无路由策略" /></div>
        </div>
      </section>

      <section v-else class="view-panel activity-view" aria-labelledby="activity-heading">
        <div class="section-heading">
          <div><span class="section-kicker">DELIVERY LEDGER</span><h2 id="activity-heading">事件与投递</h2></div>
          <a-button @click="loadActivity" :loading="activityLoading"><template #icon><RefreshCw :size="15" /></template>刷新队列</a-button>
        </div>
        <div class="activity-switcher" role="tablist">
          <button type="button" :class="{ active: activityView === 'events' }" role="tab" :aria-selected="activityView === 'events'" @click="activityView = 'events'">事件 <b>{{ events.total }}</b></button>
          <button type="button" :class="{ active: activityView === 'deliveries' }" role="tab" :aria-selected="activityView === 'deliveries'" @click="activityView = 'deliveries'">投递 <b :class="{ danger: deadDeliveryCount > 0 }">{{ deliveries.total }}</b></button>
        </div>

        <section v-if="activityView === 'events'" class="activity-table surface-panel">
          <div class="filter-row">
            <a-input v-model:value="eventFilters.event_type" allow-clear placeholder="事件类型，例如 risk.halted" @press-enter="loadActivity"><template #prefix><Activity :size="14" /></template></a-input>
            <a-select v-model:value="eventFilters.severity" :options="severityOptions" style="width: 130px" @change="loadActivity" />
            <a-select v-model:value="eventFilters.routing_status" allow-clear placeholder="路由状态" style="width: 130px" @change="loadActivity" :options="[{ value: 'routed', label: '已路由' }, { value: 'unrouted', label: '未匹配' }, { value: 'suppressed', label: '已抑制' }, { value: 'targeted', label: '定向测试' }]" />
            <a-button aria-label="应用事件筛选" @click="loadActivity"><template #icon><SlidersHorizontal :size="14" /></template>筛选</a-button>
          </div>
          <a-table :data-source="events.items" :pagination="false" row-key="id" size="small" :loading="activityLoading">
            <a-table-column key="event" title="事件"><template #default="{ record }"><div class="primary-cell"><strong>{{ record.title }}</strong><small>{{ record.event_type }} · {{ record.source }}</small></div></template></a-table-column>
            <a-table-column key="severity" title="级别" :width="88"><template #default="{ record }"><span class="severity-badge" :class="statusClass(record.severity)"><i />{{ severityLabel(record.severity) }}</span></template></a-table-column>
            <a-table-column key="route" title="路由" :width="100"><template #default="{ record }"><span class="status-text" :class="statusClass(record.routing_status)">{{ statusLabel(record.routing_status) }}</span></template></a-table-column>
            <a-table-column key="occurred" title="发生时间" :width="150"><template #default="{ record }"><time class="mono-value">{{ formatLongDate(record.occurred_at) }}</time></template></a-table-column>
          </a-table>
          <div class="table-footer"><span>共 {{ events.total }} 条事件</span><a-pagination size="small" :current="Math.floor(events.offset / events.limit) + 1" :page-size="events.limit" :total="events.total" :show-size-changer="false" @change="changeEventPage" /></div>
        </section>

        <section v-else class="activity-table surface-panel">
          <div class="filter-row">
            <a-select v-model:value="deliveryFilters.status" allow-clear placeholder="投递状态" style="width: 130px" :options="[{ value: 'pending', label: '待发送' }, { value: 'retry', label: '待重试' }, { value: 'sent', label: '已发送' }, { value: 'dead', label: '死信' }]" @change="loadActivity" />
            <a-input v-model:value="deliveryFilters.endpoint_id" allow-clear placeholder="端点 ID" @press-enter="loadActivity" />
            <a-input v-model:value="deliveryFilters.event_id" allow-clear placeholder="事件 ID" @press-enter="loadActivity" />
            <a-button aria-label="应用投递筛选" @click="loadActivity"><template #icon><SlidersHorizontal :size="14" /></template>筛选</a-button>
          </div>
          <a-table :data-source="deliveries.items" :pagination="false" row-key="id" size="small" :loading="activityLoading">
            <a-table-column key="delivery" title="投递"><template #default="{ record }"><div class="primary-cell"><strong>{{ endpointById.get(record.endpoint_id)?.name ?? record.endpoint_id.slice(0, 12) + '…' }}</strong><small>{{ connectorById.get(endpointById.get(record.endpoint_id)?.connector_id ?? '')?.name ?? '快照连接器' }}</small></div></template></a-table-column>
            <a-table-column key="status" title="状态" :width="100"><template #default="{ record }"><span class="status-text" :class="statusClass(record.status)">{{ statusLabel(record.status) }}</span></template></a-table-column>
            <a-table-column key="attempts" title="尝试" :width="74"><template #default="{ record }"><span class="mono-value">{{ record.attempt_count }}</span></template></a-table-column>
            <a-table-column key="updated" title="更新时间" :width="150"><template #default="{ record }"><time class="mono-value">{{ formatLongDate(record.updated_at) }}</time></template></a-table-column>
            <a-table-column key="actions" title="操作" :width="86"><template #default="{ record }"><a-button v-if="record.status === 'dead' || record.status === 'retry'" type="link" size="small" @click="retryDelivery(record)"><template #icon><RotateCcw :size="14" /></template>重试</a-button><span v-else class="muted-dash">—</span></template></a-table-column>
          </a-table>
          <div class="table-footer"><span>共 {{ deliveries.total }} 条投递</span><a-pagination size="small" :current="Math.floor(deliveries.offset / deliveries.limit) + 1" :page-size="deliveries.limit" :total="deliveries.total" :show-size-changer="false" @change="changeDeliveryPage" /></div>
        </section>
      </section>
    </template>

    <a-modal v-model:open="connectorModalOpen" :title="connectorEditingId ? '编辑连接器' : '新建连接器'" :confirm-loading="saving" ok-text="保存" cancel-text="取消" @ok="submitConnector">
      <a-form layout="vertical" class="modal-form">
        <a-form-item label="名称" required><a-input v-model:value="connectorForm.name" maxlength="128" placeholder="例如 ops-telegram" /></a-form-item>
        <a-form-item label="渠道类型" required><a-radio-group v-model:value="connectorForm.type" :disabled="!!connectorEditingId"><a-radio-button value="telegram"><MessageCircle :size="14" /> Telegram Bot</a-radio-button><a-radio-button value="webhook"><Globe2 :size="14" /> Webhook</a-radio-button></a-radio-group></a-form-item>
        <a-form-item :label="connectorForm.type === 'telegram' ? '密钥引用' : '密钥引用（可选）'" :required="connectorForm.type === 'telegram'"><a-input v-model:value="connectorForm.secret_ref" placeholder="Docker Secret / 环境变量名称，不直接填写密钥" /></a-form-item>
        <div v-if="connectorForm.type === 'telegram'" class="form-grid"><a-form-item label="消息格式"><a-select v-model:value="connectorForm.parse_mode" :options="[{ value: 'HTML', label: 'HTML' }, { value: 'MarkdownV2', label: 'MarkdownV2' }]" /></a-form-item></div>
        <div v-else class="form-grid"><a-form-item label="认证模式"><a-select v-model:value="connectorForm.auth_type" :options="[{ value: 'hmac_sha256', label: 'HMAC-SHA256' }, { value: 'bearer', label: 'Bearer' }, { value: 'none', label: '无认证' }]" /></a-form-item><a-form-item label="请求超时（秒）"><a-input-number v-model:value="connectorForm.timeout_seconds" :min="1" :max="60" /></a-form-item></div>
        <a-alert v-if="connectorForm.type === 'webhook' && connectorForm.auth_type !== 'none'" type="info" show-icon message="签名密钥通过 secret_ref 注入；请求会携带版本化事件封装。" />
        <a-checkbox v-if="connectorForm.type === 'webhook'" v-model:checked="connectorForm.allow_http">允许 HTTP（仅内网调试）</a-checkbox>
        <div class="form-meta"><span>配置版本 v{{ connectorForm.version }}</span><a-switch v-model:checked="connectorForm.enabled" checked-children="启用" un-checked-children="停用" /></div>
      </a-form>
    </a-modal>

    <a-modal v-model:open="endpointModalOpen" :title="endpointEditingId ? '编辑通知端点' : '添加通知端点'" :confirm-loading="saving" ok-text="保存" cancel-text="取消" @ok="submitEndpoint">
      <a-form layout="vertical" class="modal-form">
        <a-form-item label="所属连接器" required><a-select v-model:value="endpointForm.connector_id" :disabled="!!endpointEditingId" placeholder="选择发送身份" :options="connectors.map((item) => ({ value: item.id, label: `${item.name} · ${connectorLabel(item.type)}` }))" /></a-form-item>
        <a-form-item label="端点名称" required><a-input v-model:value="endpointForm.name" maxlength="128" placeholder="例如 risk-room" /></a-form-item>
        <a-form-item :label="selectedConnector?.type === 'telegram' ? 'Chat ID' : 'Webhook URL'" required><a-input v-model:value="endpointForm.address" :placeholder="selectedConnector?.type === 'telegram' ? '-1001234567890' : 'https://hooks.example.com/notify'" /></a-form-item>
        <a-form-item v-if="selectedConnector?.type === 'telegram'" label="Topic ID（可选）"><a-input v-model:value="endpointForm.topic_id" placeholder="论坛群组的 message_thread_id" /></a-form-item>
        <a-form-item v-else label="额外请求头（JSON）"><a-textarea v-model:value="endpointForm.headers_json" :rows="4" spellcheck="false" /></a-form-item>
        <div class="form-meta"><span>配置版本 v{{ endpointForm.version }}</span><a-switch v-model:checked="endpointForm.enabled" checked-children="启用" un-checked-children="停用" /></div>
      </a-form>
    </a-modal>

    <a-modal v-model:open="groupModalOpen" :title="groupEditingId ? '编辑职责组' : '新建职责组'" :confirm-loading="saving" ok-text="保存" cancel-text="取消" @ok="submitGroup">
      <a-form layout="vertical" class="modal-form">
        <a-form-item label="职责组名称" required><a-input v-model:value="groupForm.name" maxlength="128" placeholder="例如 risk-oncall" /></a-form-item>
        <a-form-item label="说明"><a-textarea v-model:value="groupForm.description" :rows="2" placeholder="说明该组负责的业务范围" /></a-form-item>
        <a-form-item label="成员端点" required><a-select v-model:value="groupForm.endpoint_ids" mode="multiple" :options="endpoints.map((item) => ({ value: item.id, label: `${item.name} · ${connectorById.get(item.connector_id)?.name ?? '未知连接器'}` }))" placeholder="选择一个或多个接收端点" /></a-form-item>
        <div class="form-meta"><span>配置版本 v{{ groupForm.version }}</span><a-switch v-model:checked="groupForm.enabled" checked-children="启用" un-checked-children="停用" /></div>
      </a-form>
    </a-modal>

    <a-modal v-model:open="policyModalOpen" :title="policyEditingId ? '编辑路由策略' : '新建路由策略'" :confirm-loading="saving" ok-text="保存" cancel-text="取消" @ok="submitPolicy">
      <a-form layout="vertical" class="modal-form">
        <a-form-item label="策略名称" required><a-input v-model:value="policyForm.name" maxlength="128" placeholder="例如 critical-ops" /></a-form-item>
        <div class="form-grid"><a-form-item label="事件模式" required><a-input v-model:value="policyForm.event_pattern" placeholder="risk.* 或精确事件名" /></a-form-item><a-form-item label="重要级别" required><a-select v-model:value="policyForm.severity" :options="severityOptions.filter((item) => item.value)" /></a-form-item></div>
        <div class="form-grid"><a-form-item label="优先级"><a-input-number v-model:value="policyForm.priority" :min="-999" :max="999" /></a-form-item><a-form-item label="职责组"><a-select v-model:value="policyForm.group_ids" mode="multiple" :disabled="policyForm.suppress" :options="groups.map((item) => ({ value: item.id, label: item.name }))" placeholder="选择通知职责组" /></a-form-item></div>
        <div class="policy-toggle"><div><strong>显式抑制</strong><small>匹配事件只记录，不创建投递任务</small></div><a-switch v-model:checked="policyForm.suppress" /></div>
        <div class="form-meta"><span>配置版本 v{{ policyForm.version }}</span><a-switch v-model:checked="policyForm.enabled" checked-children="启用" un-checked-children="停用" /></div>
      </a-form>
    </a-modal>
  </main>
</template>

<style scoped lang="scss">
.notification-page { width: 100%; max-width: 1440px; margin: 0 auto; }
.notification-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; padding-bottom: 15px; border-bottom: 1px solid var(--line); }
.heading-copy { min-width: 0; }
.eyebrow, .section-kicker { display: block; color: #b58120; font: 10px/1.3 "IBM Plex Mono", monospace; letter-spacing: .08em; }
.title-line { display: flex; align-items: center; gap: 11px; margin-top: 6px; }
.title-icon { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid rgba(59, 130, 246, .35); border-radius: 6px; color: var(--primary); background: rgba(59, 130, 246, .08); }
h1 { margin: 0; font-size: 22px; line-height: 1.2; letter-spacing: 0; }
.title-line p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.heading-actions, .section-actions { display: flex; align-items: center; gap: 8px; }
.sync-stamp { color: var(--muted); font: 11px "IBM Plex Mono", monospace; white-space: nowrap; }
.icon-button { display: inline-flex; align-items: center; gap: 6px; }
.view-switcher { display: flex; align-items: center; gap: 3px; padding: 12px 0 15px; overflow-x: auto; }
.view-tab { display: inline-flex; align-items: center; gap: 7px; min-height: 34px; padding: 0 11px; border: 1px solid transparent; border-radius: 6px; color: var(--muted); background: transparent; cursor: pointer; font: 12px "IBM Plex Sans", "Noto Sans SC", sans-serif; white-space: nowrap; transition: color .16s ease, background .16s ease, border-color .16s ease; }
.view-tab:hover { color: var(--text); background: var(--surface-hover); }
.view-tab.active { border-color: var(--line); color: var(--text); background: var(--surface); box-shadow: 0 1px 2px rgba(15, 23, 42, .04); }
.tab-count { display: inline-grid; place-items: center; min-width: 17px; height: 17px; padding: 0 4px; border-radius: 10px; color: #fff; background: #dc2626; font-size: 10px; }
.inline-alert { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; padding: 9px 12px; border: 1px solid rgba(217, 119, 6, .3); border-radius: 6px; color: #a16207; background: rgba(245, 158, 11, .09); font-size: 12px; }
.inline-alert .ant-btn { margin-left: auto; padding-inline: 4px; }
.query-state, .query-empty { display: flex; align-items: center; justify-content: center; min-height: 220px; gap: 10px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); background: var(--surface); }
.view-panel { min-width: 0; }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 10px; }
.section-heading h2 { margin: 4px 0 0; font-size: 19px; line-height: 1.25; letter-spacing: 0; }
.section-note { color: var(--muted); font-size: 11px; }
.section-description { margin: 0 0 14px; color: var(--muted); font-size: 12px; }
.metric-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 14px; overflow: hidden; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.metric-item { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }
.metric-item:last-child { border-right: 0; }
.metric-item > span, .metric-item em { display: block; color: var(--muted); font-size: 11px; font-style: normal; }
.metric-item strong { display: block; margin: 5px 0 3px; color: var(--text); font: 600 23px/1 "IBM Plex Mono", monospace; }
.metric-item strong small { margin-left: 2px; color: var(--muted); font-size: 13px; font-weight: 400; }
.metric-warning strong { color: #b45309; }
.overview-grid { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); gap: 14px; margin-bottom: 14px; }
.surface-panel { border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 43px; padding: 10px 13px; border-bottom: 1px solid var(--line); }
.panel-heading > div { display: flex; align-items: center; gap: 7px; min-width: 0; }
.panel-heading h3 { margin: 0; font-size: 13px; letter-spacing: 0; }
.panel-icon { color: var(--primary); }
.quiet-value { color: var(--muted); font-size: 11px; }
.health-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 4px 13px 7px; }
.health-list > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 0; border-bottom: 1px solid rgba(148, 163, 184, .16); color: var(--muted); font-size: 12px; }
.health-list > div:nth-last-child(-n+2) { border-bottom: 0; }
.health-list strong { color: var(--text); font: 13px "IBM Plex Mono", monospace; }
.health-list span { display: inline-flex; align-items: center; gap: 7px; }
.dot, .event-severity i, .severity-badge i { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #94a3b8; }
.dot-green, .status-sent i, .status-routed i { background: #059669; }.dot-blue, .status-pending i, .status-targeted i { background: #2563eb; }.dot-amber, .status-retry i, .status-warning i { background: #d97706; }.dot-red, .status-dead i, .status-critical i, .status-unrouted i { background: #dc2626; }.status-info i { background: #2563eb; }.status-suppressed i { background: #64748b; }
.panel-link { display: inline-flex; align-items: center; gap: 5px; padding: 6px 13px 12px; font-size: 11px; }
.panel-link span { font-size: 15px; line-height: 10px; }
.quick-panel { padding-bottom: 3px; }
.quick-action { display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 9px; width: 100%; min-height: 50px; padding: 7px 13px; border: 0; border-bottom: 1px solid rgba(148, 163, 184, .15); color: var(--text); background: transparent; text-align: left; cursor: pointer; }
.quick-action:last-child { border-bottom: 0; }.quick-action:hover { background: var(--surface-hover); }.quick-action > span:nth-child(2) { min-width: 0; }.quick-action strong, .quick-action small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.quick-action strong { font-size: 12px; }.quick-action small { margin-top: 2px; color: var(--muted); font-size: 11px; }.quick-mark { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 6px; }.quick-mark.telegram { color: #2563eb; background: rgba(37, 99, 235, .1); }.quick-mark.webhook { color: #0f766e; background: rgba(13, 148, 136, .1); }.quick-mark.policy { color: #b45309; background: rgba(217, 119, 6, .1); }.quick-arrow { color: var(--muted); font-size: 17px; }
.recent-panel { overflow: hidden; }.event-list { padding: 3px 13px; }.event-row { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto 128px; align-items: center; gap: 9px; min-height: 47px; border-bottom: 1px solid rgba(148, 163, 184, .15); }.event-row:last-child { border-bottom: 0; }.event-severity { display: grid; place-items: center; }.event-copy { min-width: 0; }.event-copy strong, .event-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.event-copy strong { font-size: 12px; }.event-copy small { margin-top: 2px; color: var(--muted); font: 10px "IBM Plex Mono", monospace; }.event-state, .status-text { white-space: nowrap; font-size: 11px; }.event-state.status-routed, .status-text.status-sent { color: #047857; }.event-state.status-unrouted, .status-text.status-dead { color: #b91c1c; }.event-state.status-targeted, .status-text.status-pending { color: #1d4ed8; }.event-state.status-suppressed { color: var(--muted); }.event-row time { color: var(--muted); font: 10px "IBM Plex Mono", monospace; text-align: right; white-space: nowrap; }
.connector-list { display: grid; gap: 12px; }.connector-block { overflow: hidden; }.connector-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 14px 11px; }.connector-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }.connector-mark { display: grid; flex: 0 0 auto; place-items: center; width: 32px; height: 32px; border-radius: 6px; }.connector-mark.telegram { color: #2563eb; background: rgba(37, 99, 235, .1); }.connector-mark.webhook { color: #0f766e; background: rgba(13, 148, 136, .1); }.connector-name { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }.connector-name strong { overflow-wrap: anywhere; font-size: 14px; }.connector-identity small { display: block; margin-top: 3px; color: var(--muted); font: 10px "IBM Plex Mono", monospace; overflow-wrap: anywhere; }.connector-actions { display: flex; align-items: center; gap: 4px; }.endpoint-head { display: flex; align-items: center; justify-content: space-between; padding: 7px 14px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .05em; }.endpoint-head > div { display: flex; align-items: center; gap: 8px; }.endpoint-head .ant-btn { height: 24px; padding-inline: 4px; font-size: 11px; text-transform: none; letter-spacing: 0; }.endpoint-list { padding: 0 14px; }.endpoint-row { display: grid; grid-template-columns: 9px minmax(0, 1fr) auto 34px 30px 30px 30px; align-items: center; gap: 9px; min-height: 50px; border-bottom: 1px solid rgba(148, 163, 184, .15); }.endpoint-row:last-child { border-bottom: 0; }.endpoint-state { width: 7px; height: 7px; border-radius: 50%; background: #94a3b8; }.endpoint-state.enabled { background: #059669; }.endpoint-copy { min-width: 0; }.endpoint-copy strong, .endpoint-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.endpoint-copy strong { font-size: 12px; }.endpoint-copy small { margin-top: 2px; color: var(--muted); font: 10px "IBM Plex Mono", monospace; }.endpoint-version, .mono-value { color: var(--muted); font: 10px "IBM Plex Mono", monospace; }.empty-inline { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 14px; color: var(--muted); font-size: 12px; }.data-table-wrap { overflow: hidden; }.data-table-wrap :deep(.ant-table-thead > tr > th) { color: var(--muted); background: var(--surface-hover); font-size: 11px; font-weight: 500; }.data-table-wrap :deep(.ant-table-tbody > tr > td), .activity-table :deep(.ant-table-tbody > tr > td) { border-bottom-color: rgba(148, 163, 184, .15); }.primary-cell { min-width: 0; }.primary-cell strong, .primary-cell small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.primary-cell strong { font-size: 12px; }.primary-cell small { margin-top: 3px; color: var(--muted); font: 10px "IBM Plex Mono", monospace; }.wrap-value { display: block; max-width: 420px; overflow-wrap: anywhere; color: var(--text); font-size: 12px; line-height: 1.45; }.table-empty { padding: 30px 0; }.pattern-code { display: inline-block; max-width: 260px; padding: 3px 6px; border: 1px solid var(--line); border-radius: 4px; color: var(--text); background: var(--surface-hover); font: 11px "IBM Plex Mono", monospace; overflow-wrap: anywhere; }.severity-badge { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; font-size: 11px; }.severity-badge.status-critical { color: #b91c1c; }.severity-badge.status-warning { color: #a16207; }.severity-badge.status-info { color: #1d4ed8; }
.activity-switcher { display: flex; align-items: center; gap: 4px; margin-bottom: 10px; }.activity-switcher button { display: inline-flex; align-items: center; gap: 7px; min-height: 30px; padding: 0 10px; border: 1px solid transparent; border-radius: 5px; color: var(--muted); background: transparent; cursor: pointer; font-size: 12px; }.activity-switcher button.active { border-color: var(--line); color: var(--text); background: var(--surface); }.activity-switcher b { min-width: 16px; padding: 1px 4px; border-radius: 8px; color: var(--muted); background: var(--surface-hover); font: 10px "IBM Plex Mono", monospace; }.activity-switcher b.danger { color: #fff; background: #dc2626; }.activity-table { overflow: hidden; }.filter-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 11px 12px; border-bottom: 1px solid var(--line); }.filter-row .ant-input { width: min(290px, 100%); }.filter-row .ant-select { min-width: 125px; }.table-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 51px; padding: 8px 13px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11px; }.muted-dash { color: var(--muted); }.modal-form { padding-top: 5px; }.modal-form :deep(.ant-form-item) { margin-bottom: 14px; }.modal-form :deep(.ant-form-item-label > label) { color: var(--muted); font-size: 11px; }.modal-form :deep(.ant-radio-button-wrapper) { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }.modal-form :deep(.ant-input-number), .modal-form :deep(.ant-select) { width: 100%; }.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }.form-meta, .policy-toggle { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 36px; padding-top: 7px; border-top: 1px solid var(--line); color: var(--muted); font: 11px "IBM Plex Mono", monospace; }.policy-toggle { margin-bottom: 12px; padding: 8px 0; border-top: 0; }.policy-toggle strong, .policy-toggle small { display: block; font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif; }.policy-toggle strong { color: var(--text); font-size: 12px; }.policy-toggle small { margin-top: 3px; color: var(--muted); font-size: 11px; }
@media (max-width: 900px) { .notification-heading, .section-heading { align-items: flex-start; flex-direction: column; }.heading-actions { width: 100%; justify-content: space-between; }.metric-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }.metric-item:nth-child(2) { border-right: 0; }.metric-item:nth-child(-n+2) { border-bottom: 1px solid var(--line); }.overview-grid { grid-template-columns: 1fr; }.section-actions { width: 100%; }.section-actions .ant-btn { flex: 1; }.event-row { grid-template-columns: 12px minmax(0, 1fr) auto; }.event-row time { display: none; }.data-table-wrap :deep(.ant-table) { min-width: 700px; }.data-table-wrap { overflow-x: auto; }.data-table-wrap :deep(.ant-table-wrapper) { min-width: 700px; } }
@media (max-width: 600px) { .notification-heading { gap: 12px; }.title-line p { max-width: 270px; line-height: 1.45; }.view-switcher { margin-inline: -2px; }.view-tab { padding-inline: 8px; }.section-note { display: none; }.metric-item { padding: 12px; }.metric-item strong { font-size: 20px; }.health-list { grid-template-columns: 1fr; }.health-list > div:nth-last-child(-n+2) { border-bottom: 1px solid rgba(148, 163, 184, .16); }.health-list > div:last-child { border-bottom: 0; }.connector-header { align-items: flex-start; }.connector-actions { flex: 0 0 auto; }.endpoint-row { grid-template-columns: 9px minmax(0, 1fr) repeat(4, 30px); gap: 5px; }.endpoint-version { display: none; }.filter-row > .ant-input, .filter-row > .ant-select, .filter-row > .ant-btn { width: 100% !important; }.table-footer { align-items: flex-start; flex-direction: column; }.form-grid { grid-template-columns: 1fr; gap: 0; } }
</style>
