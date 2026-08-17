import type {
  NotificationConnector,
  NotificationEndpoint,
  NotificationGroup,
  NotificationSeverity
} from '@/api/types'

export type NotificationViewKey = 'overview' | 'connectors' | 'groups' | 'policies' | 'activity'
export type NotificationActivityKey = 'events' | 'deliveries'

export type ConnectorForm = {
  name: string
  type: 'telegram' | 'webhook'
  secret_ref: string
  parse_mode: string
  timeout_seconds: number
  auth_type: 'none' | 'bearer' | 'hmac_sha256'
  allow_http: boolean
  enabled: boolean
  version: number
}

export type EndpointForm = {
  connector_id: string
  name: string
  address: string
  topic_id: string
  headers_json: string
  enabled: boolean
  version: number
}

export type GroupForm = {
  name: string
  description: string
  endpoint_ids: string[]
  enabled: boolean
  version: number
}

export type PolicyForm = {
  name: string
  event_pattern: string
  severity: NotificationSeverity
  priority: number
  suppress: boolean
  group_ids: string[]
  enabled: boolean
  version: number
}

export type NotificationFormProps = {
  connectorForm: ConnectorForm
  endpointForm: EndpointForm
  groupForm: GroupForm
  policyForm: PolicyForm
  connectors: NotificationConnector[]
  endpoints: NotificationEndpoint[]
  groups: NotificationGroup[]
  selectedConnector: NotificationConnector | null
  connectorById: Map<string, NotificationConnector>
  connectorEditingId: string | null
  endpointEditingId: string | null
  groupEditingId: string | null
  policyEditingId: string | null
}
