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
  Page
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

async function request<T>(
  path: string,
  init?: RequestInit & { query?: Query }
): Promise<T> {
  const { query, ...rest } = init ?? {}
  const response = await fetch(`${BASE}${path}${search(query)}`, {
    headers: { 'Content-Type': 'application/json' },
    ...rest
  })
  if (!response.ok) {
    // FastAPI 的错误体统一是 {detail: string}，取不到时退回状态码。
    const body = await response.json().catch(() => null)
    const detail =
      body && typeof body.detail === 'string'
        ? body.detail
        : `HTTP ${response.status}`
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
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

/** REST client for notification configuration and delivery operations. */
export const notificationApi = {
  overview: () => api.get<NotificationOverview>(`${NOTIFICATIONS}/overview`),
  connectors: () => api.get<Page<NotificationConnector>>(`${NOTIFICATIONS}/connectors`, { limit: 1000 }),
  createConnector: (body: NotificationConnectorInput) =>
    api.post<NotificationConnector>(`${NOTIFICATIONS}/connectors`, body),
  updateConnector: (id: string, body: NotificationConnectorInput & { expected_version: number }) =>
    api.put<NotificationConnector>(`${NOTIFICATIONS}/connectors/${encodeURIComponent(id)}`, body),
  deleteConnector: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/connectors/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  endpoints: (connectorId?: string) =>
    api.get<Page<NotificationEndpoint>>(`${NOTIFICATIONS}/endpoints`, { limit: 1000, ...(connectorId ? { connector_id: connectorId } : {}) }),
  createEndpoint: (body: NotificationEndpointInput) =>
    api.post<NotificationEndpoint>(`${NOTIFICATIONS}/endpoints`, body),
  updateEndpoint: (id: string, body: NotificationEndpointInput & { expected_version: number }) =>
    api.put<NotificationEndpoint>(`${NOTIFICATIONS}/endpoints/${encodeURIComponent(id)}`, body),
  deleteEndpoint: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/endpoints/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  testEndpoint: (id: string, body?: { title?: string; body?: string; payload?: Record<string, unknown> }) =>
    api.post<NotificationPublishResponse>(`${NOTIFICATIONS}/endpoints/${encodeURIComponent(id)}/test`, body),
  groups: () => api.get<Page<NotificationGroup>>(`${NOTIFICATIONS}/groups`, { limit: 1000 }),
  createGroup: (body: NotificationGroupInput) =>
    api.post<NotificationGroup>(`${NOTIFICATIONS}/groups`, body),
  updateGroup: (id: string, body: NotificationGroupInput & { expected_version: number }) =>
    api.put<NotificationGroup>(`${NOTIFICATIONS}/groups/${encodeURIComponent(id)}`, body),
  deleteGroup: (id: string, expectedVersion: number) =>
    api.delete<void>(`${NOTIFICATIONS}/groups/${encodeURIComponent(id)}`, { expected_version: expectedVersion }),
  policies: () => api.get<Page<NotificationPolicy>>(`${NOTIFICATIONS}/policies`, { limit: 1000 }),
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
