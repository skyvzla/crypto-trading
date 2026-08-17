CREATE TABLE notification_connectors (
    id UUID PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    type VARCHAR(16) NOT NULL CHECK (type IN ('telegram', 'webhook')),
    secret_ref VARCHAR(256),
    config JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(config) = 'object'),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE notification_endpoints (
    id UUID PRIMARY KEY,
    connector_id UUID NOT NULL
        REFERENCES notification_connectors(id) ON DELETE RESTRICT,
    name VARCHAR(128) NOT NULL CHECK (btrim(name) <> ''),
    address TEXT NOT NULL CHECK (btrim(address) <> ''),
    config JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(config) = 'object'),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (connector_id, name)
);

CREATE INDEX idx_notification_endpoints_connector
    ON notification_endpoints(connector_id, enabled);

CREATE TABLE notification_groups (
    id UUID PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE notification_group_members (
    group_id UUID NOT NULL
        REFERENCES notification_groups(id) ON DELETE CASCADE,
    endpoint_id UUID NOT NULL
        REFERENCES notification_endpoints(id) ON DELETE RESTRICT,
    PRIMARY KEY (group_id, endpoint_id)
);

CREATE INDEX idx_notification_group_members_endpoint
    ON notification_group_members(endpoint_id);

CREATE TABLE notification_policies (
    id UUID PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    event_pattern VARCHAR(160) NOT NULL CHECK (btrim(event_pattern) <> ''),
    severity VARCHAR(16) NOT NULL
        CHECK (severity IN ('info', 'warning', 'critical')),
    priority INTEGER NOT NULL DEFAULT 0,
    suppress BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notification_policies_routing
    ON notification_policies(severity, enabled, priority DESC);

CREATE TABLE notification_policy_groups (
    policy_id UUID NOT NULL
        REFERENCES notification_policies(id) ON DELETE CASCADE,
    group_id UUID NOT NULL
        REFERENCES notification_groups(id) ON DELETE RESTRICT,
    PRIMARY KEY (policy_id, group_id)
);

CREATE INDEX idx_notification_policy_groups_group
    ON notification_policy_groups(group_id);

CREATE TABLE notification_events (
    id UUID PRIMARY KEY,
    event_type VARCHAR(160) NOT NULL CHECK (btrim(event_type) <> ''),
    severity VARCHAR(16) NOT NULL
        CHECK (severity IN ('info', 'warning', 'critical')),
    source VARCHAR(160) NOT NULL CHECK (btrim(source) <> ''),
    title VARCHAR(256) NOT NULL CHECK (btrim(title) <> ''),
    body TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(payload) = 'object'),
    idempotency_key VARCHAR(256) NOT NULL
        CHECK (btrim(idempotency_key) <> ''),
    correlation_id VARCHAR(256),
    fingerprint VARCHAR(256),
    matched_policy_id UUID
        REFERENCES notification_policies(id) ON DELETE SET NULL,
    routing_status VARCHAR(16) NOT NULL
        CHECK (routing_status IN ('pending', 'routed', 'suppressed', 'unrouted', 'targeted')),
    occurred_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at IS NULL OR expires_at > occurred_at),
    UNIQUE (source, idempotency_key)
);

CREATE INDEX idx_notification_events_created
    ON notification_events(created_at DESC);
CREATE INDEX idx_notification_events_type_severity
    ON notification_events(event_type, severity, created_at DESC);
CREATE INDEX idx_notification_events_routing
    ON notification_events(routing_status, created_at DESC);

CREATE TABLE notification_deliveries (
    id UUID PRIMARY KEY,
    event_id UUID NOT NULL
        REFERENCES notification_events(id) ON DELETE CASCADE,
    endpoint_id UUID NOT NULL
        REFERENCES notification_endpoints(id) ON DELETE RESTRICT,
    connector_snapshot JSONB NOT NULL
        CHECK (jsonb_typeof(connector_snapshot) = 'object'),
    endpoint_snapshot JSONB NOT NULL
        CHECK (jsonb_typeof(endpoint_snapshot) = 'object'),
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'retry', 'sent', 'dead')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    lease_owner VARCHAR(160),
    last_error TEXT,
    provider_message_id VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    UNIQUE (event_id, endpoint_id),
    CHECK (
        (status = 'sending' AND lease_until IS NOT NULL AND lease_owner IS NOT NULL)
        OR (status <> 'sending' AND lease_until IS NULL AND lease_owner IS NULL)
    )
);

CREATE INDEX idx_notification_deliveries_due
    ON notification_deliveries(status, next_attempt_at)
    WHERE status IN ('pending', 'retry');
CREATE INDEX idx_notification_deliveries_event
    ON notification_deliveries(event_id, created_at);
CREATE INDEX idx_notification_deliveries_endpoint
    ON notification_deliveries(endpoint_id, created_at DESC);
