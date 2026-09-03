import type { NotificationConnector, NotificationEndpoint } from '@/api/types'
import { EMPTY_VALUE } from '@/shared/format'
import { formatLedgerDateTime, formatLedgerShortDateTime } from '@/shared/time'

/**
 * 通知中心的展示映射。
 *
 * 这些是纯函数，子组件直接 import 即可——不要再通过 props 往下传函数，
 * 那会让每个面板都必须由父页面注入才能渲染。
 */

export type BadgeStatus = 'success' | 'processing' | 'warning' | 'error' | 'default'

const SEVERITY_LABELS: Record<string, string> = {
  info: '信息',
  warning: '预警',
  critical: '严重',
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待发送',
  sending: '发送中',
  retry: '待重试',
  sent: '已发送',
  dead: '死信',
  routed: '已路由',
  suppressed: '已抑制',
  unrouted: '未匹配',
  targeted: '定向测试',
}

/** 事件级别与投递状态共用一套徽标色，保证同一语义在各处颜色一致。 */
const BADGE_STATUSES: Record<string, BadgeStatus> = {
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
  suppressed: 'default',
}

export interface SeverityOption {
  value: string
  label: string
}

export const SEVERITY_OPTIONS: SeverityOption[] = [
  { value: '', label: '全部级别' },
  { value: 'info', label: '信息' },
  { value: 'warning', label: '预警' },
  { value: 'critical', label: '严重' },
]

export function severityLabel(value: string): string {
  return SEVERITY_LABELS[value] ?? value
}

export function statusLabel(value: string): string {
  return STATUS_LABELS[value] ?? value
}

export function statusBadge(value: string): BadgeStatus {
  return BADGE_STATUSES[value] ?? 'default'
}

export function connectorLabel(type: string): string {
  return type === 'telegram' ? 'Telegram Bot' : 'Webhook'
}

/** 列表用短时间（省略年份与秒）。 */
export function formatShortTime(value: string | null | undefined): string {
  return formatLedgerShortDateTime(value) ?? EMPTY_VALUE
}

/** 表格用完整时间。 */
export function formatFullTime(value: string | null | undefined): string {
  return formatLedgerDateTime(value) ?? EMPTY_VALUE
}

export function connectorConfigSummary(connector: NotificationConnector): string {
  if (connector.type === 'telegram') {
    return connector.secret_ref ? `密钥：${connector.secret_ref}` : '未绑定密钥引用'
  }
  const timeout = connector.config?.timeout_seconds
  return timeout ? `超时 ${timeout}s` : '标准 HTTP webhook'
}

export function endpointConfigSummary(
  endpoint: NotificationEndpoint,
  connector: NotificationConnector | undefined,
): string {
  if (connector?.type === 'telegram') {
    const topic = endpoint.config?.message_thread_id ?? endpoint.config?.topic_id
    return topic ? `chat ${endpoint.address} · Topic ${topic}` : `chat ${endpoint.address}`
  }
  return endpoint.address
}

/** 把 id 列表渲染成可读名称；缺失的 id 退化成短前缀而不是整段 UUID。 */
export function nameList(ids: string[], lookup: Map<string, { name: string }>, emptyText: string): string {
  return ids.map((id) => lookup.get(id)?.name ?? `${id.slice(0, 8)}…`).join('、') || emptyText
}
