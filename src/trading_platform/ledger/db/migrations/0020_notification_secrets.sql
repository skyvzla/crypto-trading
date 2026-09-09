ALTER TABLE notification_connectors
    ADD COLUMN legacy_secret_ref TEXT;

-- Before 0020, ``file:/...`` references could point at arbitrary absolute
-- paths. Preserve those values for existing workers while keeping the new
-- user-writable reference column restricted to environment/Docker secrets.
UPDATE notification_connectors
SET legacy_secret_ref = secret_ref,
    secret_ref = NULL
WHERE secret_ref IS NOT NULL
  AND secret_ref ~ '^file:/'
  AND secret_ref !~ '^file:/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]*$';

UPDATE notification_deliveries
SET connector_snapshot = jsonb_set(
        jsonb_set(
            connector_snapshot,
            '{legacy_secret_ref}',
            to_jsonb(connector_snapshot ->> 'secret_ref'),
            TRUE
        ),
        '{secret_ref}',
        'null'::JSONB,
        TRUE
    )
WHERE connector_snapshot ->> 'secret_ref' ~ '^file:/'
  AND connector_snapshot ->> 'secret_ref' !~ '^file:/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]*$';

UPDATE notification_connectors
SET secret_ref = NULL
WHERE secret_ref IS NOT NULL AND btrim(secret_ref) = '';

ALTER TABLE notification_connectors
    ADD CONSTRAINT notification_connectors_secret_ref_check CHECK (
        secret_ref IS NULL
        OR secret_ref ~ '^env:[A-Za-z_][A-Za-z0-9_]*$'
        OR secret_ref ~ '^file:/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]*$'
        OR secret_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    );

CREATE TABLE notification_secrets (
    connector_id UUID PRIMARY KEY
        REFERENCES notification_connectors(id) ON DELETE CASCADE,
    secret_value TEXT NOT NULL CHECK (btrim(secret_value) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
