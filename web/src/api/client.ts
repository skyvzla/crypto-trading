import type {
  NotificationConnector,
  NotificationConnectorInput,
  NotificationEndpoint,
  NotificationEndpointInput,
  NotificationEvent,
  NotificationGroup,
  NotificationGroupInput,
  NotificationOverview,
  NotificationPolicy,
  NotificationPolicyInput,
  NotificationDelivery,
  NotificationPublishResponse,
  Page,
  PageParams
} from '@/api/types'

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

function search(query?: Query): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

/**
 * 读取响应体文本。
 *
 * 204/205 按协议没有 body；DELETE 之类的接口也可能返回 200 但 body 为空。
 * 统一先取文本再决定是否解析，避免直接调用 `response.json()` 抛 SyntaxError。
 */
async function readBody(response: Response): Promise<string> {
  if (response.status === 204 || response.status === 205) return ''
  return response.text().catch(() => '')
}

/** FastAPI 的错误体统一是 `{detail: string}`；取不到时返回 null 交给调用方退回状态码。 */
function parseErrorDetail(body: string): string | null {
  if (!body) return null
  try {
    const parsed: unknown = JSON.parse(body)
    return parsed && typeof parsed === 'object' && typeof (parsed as { detail?: unknown }).detail === 'string'
      ? (parsed as { detail: string }).detail
      : null
  } catch {
    return null
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { query?: Query }
): Promise<T> {
  const { query, ...rest } = init ?? {}
  const response = await fetch(`${BASE}${path}${search(query)}`, {
    headers: { 'Content-Type': 'application/json' },
    ...rest
  })
  const body = await readBody(response)
  if (!response.ok) {
    throw new ApiError(response.status, parseErrorDetail(body) ?? `HTTP ${response.status}`)
  }
  if (!body) return undefined as T
  try {
    return JSON.parse(body) as T
  } catch {
    // 200 但 body 不是合法 JSON 属于服务端故障，包装成 ApiError 让页面统一展示。
    throw new ApiError(response.status, '服务端返回了无法解析的响应体')
  }
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>(path, { query }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    }),
  delete: <T>(path: string, query?: Query) =>
    request<T>(path, { method: 'DELETE', query }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
}

const NOTIFICATIONS = '/notifications'

/**
 * REST client for notification configuration and delivery operations.
 *
 * 配置类列表接受标准分页参数，由调用方用 collectPageItems 逐页取全，
 * 而不是在这里硬编码一个「足够大」的 limit 假装不用分页。
 */
export const notificationApi = {
  overview: () => api.get<NotificationOverview>(`${NOTIFICATIONS}/overview`),
  connectors: (query: PageParams = {}) =>
    api.get<Page<NotificationConnector>>(`${NOTIFICATIONS}/connectors`, { ...query }),
  createConnector: (body: NotificationConnectorInput) =>
    api.post<NotificationConnector>(`${NOTIFICATIONS}/connectors`, body),
  updateConnector: (id: string, body: NotificationConnectorInput & { expected_version: number }) =>
    api.put<NotificationConnector>(`${NOTIFICATIONS}/connectors/${encodeURIComponent(id)}`, body),
  deleteConnector: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/connectors/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  endpoints: (query: PageParams & { connector_id?: string } = {}) =>
    api.get<Page<NotificationEndpoint>>(`${NOTIFICATIONS}/endpoints`, { ...query }),
  createEndpoint: (body: NotificationEndpointInput) =>
    api.post<NotificationEndpoint>(`${NOTIFICATIONS}/endpoints`, body),
  updateEndpoint: (id: string, body: NotificationEndpointInput & { expected_version: number }) =>
    api.put<NotificationEndpoint>(`${NOTIFICATIONS}/endpoints/${encodeURIComponent(id)}`, body),
  deleteEndpoint: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/endpoints/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  testEndpoint: (id: string, body?: { title?: string; body?: string; payload?: Record<string, unknown> }) =>
    api.post<NotificationPublishResponse>(`${NOTIFICATIONS}/endpoints/${encodeURIComponent(id)}/test`, body),
  groups: (query: PageParams = {}) =>
    api.get<Page<NotificationGroup>>(`${NOTIFICATIONS}/groups`, { ...query }),
  createGroup: (body: NotificationGroupInput) =>
    api.post<NotificationGroup>(`${NOTIFICATIONS}/groups`, body),
  updateGroup: (id: string, body: NotificationGroupInput & { expected_version: number }) =>
    api.put<NotificationGroup>(`${NOTIFICATIONS}/groups/${encodeURIComponent(id)}`, body),
  deleteGroup: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/groups/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  policies: (query: PageParams = {}) =>
    api.get<Page<NotificationPolicy>>(`${NOTIFICATIONS}/policies`, { ...query }),
  createPolicy: (body: NotificationPolicyInput) =>
    api.post<NotificationPolicy>(`${NOTIFICATIONS}/policies`, body),
  updatePolicy: (id: string, body: NotificationPolicyInput & { expected_version: number }) =>
    api.put<NotificationPolicy>(`${NOTIFICATIONS}/policies/${encodeURIComponent(id)}`, body),
  deletePolicy: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/policies/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  events: (query?: { limit?: number; offset?: number; event_type?: string; severity?: string; routing_status?: string }) =>
    api.get<Page<NotificationEvent>>(`${NOTIFICATIONS}/events`, query),
  deliveries: (query?: { limit?: number; offset?: number; status?: string; event_id?: string; endpoint_id?: string }) =>
    api.get<Page<NotificationDelivery>>(`${NOTIFICATIONS}/deliveries`, query),
  retryDelivery: (id: string) =>
    api.post<NotificationDelivery>(`${NOTIFICATIONS}/deliveries/${encodeURIComponent(id)}/retry`)
}
