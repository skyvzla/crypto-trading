import { computed, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { notificationApi } from '@/api/client'
import type {
  JsonObject,
  NotificationConnector,
  NotificationConnectorInput,
  NotificationDelivery,
  NotificationEndpoint,
  NotificationEndpointInput,
  NotificationGroup,
  NotificationGroupInput,
  NotificationOverview,
  NotificationPolicy,
  NotificationPolicyInput,
  NotificationSeverity,
} from '@/api/types'
import { useNotificationActivity } from './useNotificationActivity'
import { useNotificationNavigation } from './useNotificationNavigation'
import { errorMessage, useVersionedCollection } from './useVersionedCollection'
import { collectPageItems } from '@/shared/pagination'

/** 连接器默认请求超时（秒）。 */
const DEFAULT_TIMEOUT_SECONDS = 8

const EMPTY_OVERVIEW: NotificationOverview = {
  connectors: 0,
  enabled_connectors: 0,
  endpoints: 0,
  enabled_endpoints: 0,
  groups: 0,
  policies: 0,
  events: 0,
  unrouted_events: 0,
  deliveries: { pending: 0, sending: 0, retry: 0, sent: 0, dead: 0 },
}

/**
 * 后端概览字段名历史上有过变体（deliveries / delivery_statuses），
 * 这里统一收敛成一种形状，页面不必到处做兼容判断。
 */
function normalizeOverview(value: unknown): NotificationOverview {
  const source = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  const deliveries = (source.deliveries ?? source.delivery_statuses ?? {}) as Record<string, unknown>
  const count = (input: unknown) => Number(input ?? 0)
  return {
    connectors: count(source.connectors),
    enabled_connectors: count(source.enabled_connectors),
    endpoints: count(source.endpoints),
    enabled_endpoints: count(source.enabled_endpoints),
    groups: count(source.groups),
    policies: count(source.policies),
    events: count(source.events),
    unrouted_events: count(source.unrouted_events),
    deliveries: {
      pending: count(deliveries.pending),
      sending: count(deliveries.sending),
      retry: count(deliveries.retry),
      sent: count(deliveries.sent),
      dead: count(deliveries.dead),
    },
  }
}

/** Webhook 地址必须是 HTTPS，且不能把凭据塞进 URL。 */
function assertValidEndpointAddress(connector: NotificationConnector, address: string): void {
  if (connector.type === 'telegram') return
  let parsed: URL
  try {
    parsed = new URL(address)
  } catch {
    throw new Error('请输入合法的 Webhook URL')
  }
  const allowHttp = Boolean(connector.config?.allow_http)
  if (parsed.protocol !== 'https:' && !(allowHttp && parsed.protocol === 'http:')) {
    throw new Error('Webhook 默认必须使用 HTTPS；仅允许已开启内网调试时使用 HTTP')
  }
  if (parsed.username || parsed.password) throw new Error('Webhook URL 不应包含账号或密码')
}

function parseJsonObject(value: string): JsonObject {
  if (!value.trim()) return {}
  try {
    const parsed: unknown = JSON.parse(value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('必须是 JSON 对象')
    return parsed as JsonObject
  } catch {
    throw new Error('请求头必须是合法的 JSON 对象')
  }
}

export function useNotificationWorkbench() {
  const { view, activityView, openActivity } = useNotificationNavigation()

  const loading = ref(false)
  const saving = ref(false)
  const lastSyncedAt = ref<Date | null>(null)
  const loadError = ref('')

  const connectors = ref<NotificationConnector[]>([])
  const endpoints = ref<NotificationEndpoint[]>([])
  const groups = ref<NotificationGroup[]>([])
  const policies = ref<NotificationPolicy[]>([])
  const overview = ref<NotificationOverview>({ ...EMPTY_OVERVIEW })

  async function refreshOverview() {
    try {
      overview.value = normalizeOverview(await notificationApi.overview())
    } catch (error) {
      message.warning(errorMessage(error, '通知概览刷新失败'))
    }
  }

  const activity = useNotificationActivity({
    setError: (text) => {
      loadError.value = text
    },
  })

  const connectorById = computed(() => new Map(connectors.value.map((item) => [item.id, item])))
  const endpointById = computed(() => new Map(endpoints.value.map((item) => [item.id, item])))
  const groupById = computed(() => new Map(groups.value.map((item) => [item.id, item])))
  const enabledEndpointCount = computed(() => endpoints.value.filter((item) => item.enabled).length)
  const deadDeliveryCount = computed(() => overview.value.deliveries.dead ?? 0)
  const retryDeliveryCount = computed(
    () => (overview.value.deliveries.retry ?? 0) + (overview.value.deliveries.pending ?? 0),
  )

  // ── 表单状态 ───────────────────────────────────────────────────────────
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
    timeout_seconds: DEFAULT_TIMEOUT_SECONDS,
    auth_type: 'hmac_sha256' as 'none' | 'bearer' | 'hmac_sha256',
    allow_http: false,
    enabled: true,
    version: 1,
  })
  const endpointForm = reactive({
    connector_id: '',
    name: '',
    address: '',
    topic_id: '',
    headers_json: '{}',
    enabled: true,
    version: 1,
  })
  const groupForm = reactive({ name: '', description: '', endpoint_ids: [] as string[], enabled: true, version: 1 })
  const policyForm = reactive({
    name: '',
    event_pattern: '',
    severity: 'warning' as NotificationSeverity,
    priority: 0,
    suppress: false,
    group_ids: [] as string[],
    enabled: true,
    version: 1,
  })

  const selectedConnector = computed(() => connectorById.value.get(endpointForm.connector_id) ?? null)

  // ── 集合写操作 ─────────────────────────────────────────────────────────
  const connectorCollection = useVersionedCollection({
    items: connectors,
    label: '连接器',
    update: (item, enabled) =>
      notificationApi.updateConnector(item.id, {
        name: item.name,
        type: item.type,
        secret_ref: item.secret_ref,
        config: item.config,
        enabled,
        expected_version: item.version,
      }),
    remove: (item) => notificationApi.deleteConnector(item.id, item.version),
    removeHint: '连接器删除失败；请确认没有关联端点',
    // 端点隶属于连接器，连接器没了本地也要一起清掉。
    onRemoved: (item) => {
      endpoints.value = endpoints.value.filter((row) => row.connector_id !== item.id)
    },
    afterChange: refreshOverview,
  })

  const endpointCollection = useVersionedCollection({
    items: endpoints,
    label: '端点',
    update: (item, enabled) =>
      notificationApi.updateEndpoint(item.id, {
        connector_id: item.connector_id,
        name: item.name,
        address: item.address,
        config: item.config,
        enabled,
        expected_version: item.version,
      }),
    remove: (item) => notificationApi.deleteEndpoint(item.id, item.version),
    removeHint: '端点删除失败；请先移出职责组',
    onRemoved: (item) => {
      groups.value = groups.value.map((row) => ({
        ...row,
        endpoint_ids: row.endpoint_ids.filter((id) => id !== item.id),
      }))
    },
    afterChange: refreshOverview,
  })

  const groupCollection = useVersionedCollection({
    items: groups,
    label: '职责组',
    update: (item, enabled) =>
      notificationApi.updateGroup(item.id, {
        name: item.name,
        description: item.description,
        endpoint_ids: item.endpoint_ids,
        enabled,
        expected_version: item.version,
      }),
    remove: (item) => notificationApi.deleteGroup(item.id, item.version),
    removeHint: '职责组删除失败；请先移出路由策略',
    onRemoved: (item) => {
      policies.value = policies.value.map((row) => ({
        ...row,
        group_ids: row.group_ids.filter((id) => id !== item.id),
      }))
    },
    afterChange: refreshOverview,
  })

  const policyCollection = useVersionedCollection({
    items: policies,
    label: '路由策略',
    update: (item, enabled) =>
      notificationApi.updatePolicy(item.id, {
        name: item.name,
        event_pattern: item.event_pattern,
        severity: item.severity,
        priority: item.priority,
        suppress: item.suppress,
        group_ids: item.group_ids,
        enabled,
        expected_version: item.version,
      }),
    remove: (item) => notificationApi.deletePolicy(item.id, item.version),
    afterChange: refreshOverview,
  })

  // ── 加载 ───────────────────────────────────────────────────────────────
  async function loadAll() {
    loading.value = true
    loadError.value = ''
    // 配置类列表逐页取全（数量小但必须完整：职责组要能列出所有端点）；
    // 事件与投递是时间流，只取第一页。
    const results = await Promise.allSettled([
      notificationApi.overview(),
      collectPageItems((params) => notificationApi.connectors(params)),
      collectPageItems((params) => notificationApi.endpoints(params)),
      collectPageItems((params) => notificationApi.groups(params)),
      collectPageItems((params) => notificationApi.policies(params)),
      notificationApi.events({ limit: activity.pageSize, offset: 0 }),
      notificationApi.deliveries({ limit: activity.pageSize, offset: 0 }),
    ])
    const [
      overviewResult,
      connectorsResult,
      endpointsResult,
      groupsResult,
      policiesResult,
      eventsResult,
      deliveriesResult,
    ] = results
    if (overviewResult.status === 'fulfilled') overview.value = normalizeOverview(overviewResult.value)
    if (connectorsResult.status === 'fulfilled') connectors.value = connectorsResult.value.items
    if (endpointsResult.status === 'fulfilled') endpoints.value = endpointsResult.value.items
    if (groupsResult.status === 'fulfilled') groups.value = groupsResult.value.items
    if (policiesResult.status === 'fulfilled') policies.value = policiesResult.value.items
    if (eventsResult.status === 'fulfilled') activity.events.value = eventsResult.value
    if (deliveriesResult.status === 'fulfilled') activity.deliveries.value = deliveriesResult.value
    if (results.some((item) => item.status === 'rejected')) {
      loadError.value = '部分通知数据暂时不可用，请刷新重试。'
    }
    lastSyncedAt.value = new Date()
    loading.value = false
  }

  /** 刷新当前那张表，保持在当前页。 */
  async function loadActivity() {
    if (activityView.value === 'events') await activity.loadEvents()
    else await activity.loadDeliveries()
  }

  /**
   * 应用筛选条件，并回到第一页。
   *
   * 不能沿用当前 offset：新条件下的结果集通常更短，
   * 留在第 3 页很可能直接落到一片空白上。
   */
  async function applyActivityFilters() {
    if (activityView.value === 'events') await activity.loadEvents(0)
    else await activity.loadDeliveries(0)
  }

  // ── 表单预填 ───────────────────────────────────────────────────────────
  function resetConnectorForm(item?: NotificationConnector, initialType?: 'telegram' | 'webhook') {
    connectorEditingId.value = item?.id ?? null
    Object.assign(connectorForm, {
      name: item?.name ?? '',
      type: item?.type ?? initialType ?? 'telegram',
      secret_ref: item?.secret_ref ?? '',
      parse_mode: String(item?.config?.parse_mode ?? 'HTML'),
      timeout_seconds: Number(item?.config?.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS),
      auth_type: String(item?.config?.auth_type ?? 'hmac_sha256') as 'none' | 'bearer' | 'hmac_sha256',
      allow_http: Boolean(item?.config?.allow_http ?? false),
      enabled: item?.enabled ?? true,
      version: item?.version ?? 1,
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
      version: item?.version ?? 1,
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
      version: item?.version ?? 1,
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
      version: item?.version ?? 1,
    })
    policyModalOpen.value = true
  }

  /** 保存的公共流程：置 saving、区分新建/编辑、回写列表、关窗、刷新概览。 */
  async function submitResource<TInput, TResult extends { id: string; enabled: boolean; version: number }>(options: {
    editingId: string | null
    body: TInput
    create: (body: TInput) => Promise<TResult>
    update: (id: string, body: TInput & { expected_version: number }) => Promise<TResult>
    version: number
    upsert: (result: TResult, editingId: string | null) => void
    close: () => void
    label: string
  }) {
    saving.value = true
    try {
      const editingId = options.editingId
      const result = editingId
        ? await options.update(editingId, { ...options.body, expected_version: options.version })
        : await options.create(options.body)
      options.upsert(result, editingId)
      options.close()
      message.success(editingId ? `${options.label}已更新` : `${options.label}已创建`)
      await refreshOverview()
    } catch (error) {
      message.error(errorMessage(error, `${options.label}保存失败`))
    } finally {
      saving.value = false
    }
  }

  async function submitConnector() {
    if (!connectorForm.name.trim()) return message.error('请输入连接器名称')
    if ((connectorForm.type === 'telegram' || connectorForm.auth_type !== 'none') && !connectorForm.secret_ref.trim()) {
      return message.error('当前认证模式需要密钥引用')
    }
    const body: NotificationConnectorInput = {
      name: connectorForm.name.trim(),
      type: connectorForm.type,
      secret_ref: connectorForm.secret_ref.trim(),
      enabled: connectorForm.enabled,
      config:
        connectorForm.type === 'telegram'
          ? { parse_mode: connectorForm.parse_mode || 'HTML' }
          : {
              timeout_seconds: Number(connectorForm.timeout_seconds) || DEFAULT_TIMEOUT_SECONDS,
              auth_type: connectorForm.auth_type,
              allow_http: connectorForm.allow_http,
            },
    }
    await submitResource({
      editingId: connectorEditingId.value,
      body,
      version: connectorForm.version,
      create: notificationApi.createConnector,
      update: notificationApi.updateConnector,
      upsert: connectorCollection.upsert,
      close: () => {
        connectorModalOpen.value = false
      },
      label: '连接器',
    })
  }

  async function submitEndpoint() {
    if (!endpointForm.connector_id) return message.error('请选择连接器')
    if (!endpointForm.name.trim() || !endpointForm.address.trim()) return message.error('请填写端点名称和地址')
    const connector = connectorById.value.get(endpointForm.connector_id)
    if (!connector) return message.error('连接器不存在，请刷新后重试')

    let config: JsonObject = {}
    try {
      assertValidEndpointAddress(connector, endpointForm.address.trim())
      if (connector.type === 'telegram') {
        if (endpointForm.topic_id.trim()) {
          config.message_thread_id = Number(endpointForm.topic_id) || endpointForm.topic_id.trim()
        }
      } else {
        config = { headers: parseJsonObject(endpointForm.headers_json) }
      }
    } catch (error) {
      return message.error(errorMessage(error, '端点配置格式错误'))
    }

    const body: NotificationEndpointInput = {
      connector_id: endpointForm.connector_id,
      name: endpointForm.name.trim(),
      address: endpointForm.address.trim(),
      config,
      enabled: endpointForm.enabled,
    }
    await submitResource({
      editingId: endpointEditingId.value,
      body,
      version: endpointForm.version,
      create: notificationApi.createEndpoint,
      update: notificationApi.updateEndpoint,
      upsert: endpointCollection.upsert,
      close: () => {
        endpointModalOpen.value = false
      },
      label: '端点',
    })
  }

  async function submitGroup() {
    if (!groupForm.name.trim()) return message.error('请输入职责组名称')
    if (!groupForm.endpoint_ids.length) return message.error('至少选择一个通知端点')
    const body: NotificationGroupInput = {
      name: groupForm.name.trim(),
      description: groupForm.description.trim() || null,
      endpoint_ids: [...groupForm.endpoint_ids],
      enabled: groupForm.enabled,
    }
    await submitResource({
      editingId: groupEditingId.value,
      body,
      version: groupForm.version,
      create: notificationApi.createGroup,
      update: notificationApi.updateGroup,
      upsert: groupCollection.upsert,
      close: () => {
        groupModalOpen.value = false
      },
      label: '职责组',
    })
  }

  async function submitPolicy() {
    if (!policyForm.name.trim() || !policyForm.event_pattern.trim()) return message.error('请填写策略名称和事件模式')
    if (!policyForm.suppress && !policyForm.group_ids.length) return message.error('非抑制策略至少选择一个职责组')
    const body: NotificationPolicyInput = {
      name: policyForm.name.trim(),
      event_pattern: policyForm.event_pattern.trim(),
      severity: policyForm.severity,
      priority: Number(policyForm.priority) || 0,
      suppress: policyForm.suppress,
      group_ids: [...policyForm.group_ids],
      enabled: policyForm.enabled,
    }
    await submitResource({
      editingId: policyEditingId.value,
      body,
      version: policyForm.version,
      create: notificationApi.createPolicy,
      update: notificationApi.updatePolicy,
      upsert: policyCollection.upsert,
      close: () => {
        policyModalOpen.value = false
      },
      label: '路由策略',
    })
  }

  async function testEndpoint(item: NotificationEndpoint) {
    try {
      const result = await notificationApi.testEndpoint(item.id)
      activity.prependTestResult(result.event ?? null, result.deliveries ?? [])
      message.success(`测试通知已进入队列：${item.name}`)
      await refreshOverview()
    } catch (error) {
      message.error(errorMessage(error, '测试通知发送失败'))
    }
  }

  async function retryDelivery(item: NotificationDelivery) {
    try {
      activity.replaceDelivery(await notificationApi.retryDelivery(item.id))
      message.success('投递已重新排队')
      await refreshOverview()
    } catch (error) {
      message.error(errorMessage(error, '投递重试失败'))
    }
  }

  function closeDialogs() {
    connectorModalOpen.value = false
    endpointModalOpen.value = false
    groupModalOpen.value = false
    policyModalOpen.value = false
  }

  watch([view, activityView], ([nextView]) => {
    if (nextView === 'activity') void loadActivity()
  })

  // 页面被 KeepAlive 缓存，离开时必须收起弹窗，否则回来还挂在屏幕上。
  onDeactivated(closeDialogs)
  onMounted(() => {
    void loadAll()
  })

  return {
    // 导航
    view,
    activityView,
    openActivity,
    // 状态
    loading,
    saving,
    lastSyncedAt,
    loadError,
    connectors,
    endpoints,
    groups,
    policies,
    overview,
    connectorById,
    endpointById,
    groupById,
    enabledEndpointCount,
    deadDeliveryCount,
    retryDeliveryCount,
    selectedConnector,
    // 事件与投递
    events: activity.events,
    deliveries: activity.deliveries,
    eventFilters: activity.eventFilters,
    deliveryFilters: activity.deliveryFilters,
    activityLoading: activity.activityLoading,
    eventsLoading: activity.eventsLoading,
    deliveriesLoading: activity.deliveriesLoading,
    changeEventPage: activity.changeEventPage,
    changeDeliveryPage: activity.changeDeliveryPage,
    loadAll,
    loadActivity,
    applyActivityFilters,
    // 表单
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
    resetConnectorForm,
    resetEndpointForm,
    resetGroupForm,
    resetPolicyForm,
    submitConnector,
    submitEndpoint,
    submitGroup,
    submitPolicy,
    closeDialogs,
    // 集合写操作
    toggleConnector: connectorCollection.toggle,
    toggleEndpoint: endpointCollection.toggle,
    toggleGroup: groupCollection.toggle,
    togglePolicy: policyCollection.toggle,
    deleteConnector: connectorCollection.destroy,
    deleteEndpoint: endpointCollection.destroy,
    deleteGroup: groupCollection.destroy,
    deletePolicy: policyCollection.destroy,
    testEndpoint,
    retryDelivery,
  }
}
