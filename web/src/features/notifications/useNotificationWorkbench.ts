import { computed, nextTick, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { notificationApi } from '@/api/client'
import { onBeforeRouteLeave } from 'vue-router'
import { router } from '@/router'
import type { NotificationActivityKey, NotificationViewKey } from './types'
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

export function useNotificationWorkbench() {
  const viewRouteNames: Record<NotificationViewKey, string> = {
    overview: 'notifications',
    connectors: 'notifications-connectors',
    groups: 'notifications-groups',
    policies: 'notifications-policies',
    activity: 'notifications-activity'
  }
  const view = computed<NotificationViewKey>({
    get: () => Object.entries(viewRouteNames).find(([, name]) => name === String(router.currentRoute.value.name))?.[0] as NotificationViewKey ?? 'overview',
    set: (next) => { if (String(router.currentRoute.value.name) !== viewRouteNames[next]) void router.push({ name: viewRouteNames[next] }) }
  })
  const activityView = computed<NotificationActivityKey>({
    get: () => router.currentRoute.value.query.tab === 'deliveries' ? 'deliveries' : 'events',
    set: (next) => { void router.push({ name: viewRouteNames.activity, query: { ...router.currentRoute.value.query, tab: next } }) }
  })
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

  function statusBadge(value: string) {
    const statuses = {
      sent: 'success',
      routed: 'success',
      pending: 'processing',
      sending: 'processing',
      targeted: 'processing',
      info: 'processing',
      retry: 'warning',
      warning: 'warning',
      dead: 'error',
      critical: 'error',
      unrouted: 'error',
      suppressed: 'default'
    } as const
    return statuses[value as keyof typeof statuses] ?? 'default'
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

  function resetConnectorForm(item?: NotificationConnector, initialType?: 'telegram' | 'webhook') {
    connectorEditingId.value = item?.id ?? null
    Object.assign(connectorForm, {
      name: item?.name ?? '',
      type: item?.type ?? initialType ?? 'telegram',
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

  function navigateView(next: NotificationViewKey) { view.value = next }
  function openActivity(next: NotificationActivityKey) {
    void router.push({ name: viewRouteNames.activity, query: { tab: next } })
  }
  function setActivityView(next: NotificationActivityKey) { activityView.value = next }
  function openConnectorForm(type?: 'telegram' | 'webhook') { resetConnectorForm(undefined, type) }
  function openEndpointForm(connectorId?: string) { resetEndpointForm(undefined, connectorId) }
  function setConnectorModalOpen(value: boolean) { connectorModalOpen.value = value }
  function setEndpointModalOpen(value: boolean) { endpointModalOpen.value = value }
  function setGroupModalOpen(value: boolean) { groupModalOpen.value = value }
  function setPolicyModalOpen(value: boolean) { policyModalOpen.value = value }

  function closeDialogs() {
    connectorModalOpen.value = false
    endpointModalOpen.value = false
    groupModalOpen.value = false
    policyModalOpen.value = false
  }

  watch([view, activityView], ([nextView]) => {
    if (nextView === 'activity') void loadActivity()
  })

  onBeforeRouteLeave(async () => {
    closeDialogs()
    await nextTick()
  })
  onDeactivated(closeDialogs)
  onMounted(() => { void loadAll() })

  return {
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
  }
}
