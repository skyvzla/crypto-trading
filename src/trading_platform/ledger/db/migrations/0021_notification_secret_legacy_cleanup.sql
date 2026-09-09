-- 0020 only moved arbitrary absolute file references. Move every other
-- pre-0020 reference before enforcing the 0020 allowlist in new writes.
UPDATE notification_connectors
SET legacy_secret_ref = secret_ref,
    secret_ref = NULL
WHERE secret_ref IS NOT NULL
  AND btrim(secret_ref) <> ''
  AND secret_ref !~ '^env:[A-Za-z_][A-Za-z0-9_]*$'
  AND secret_ref !~ '^file:/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]*$'
  AND secret_ref !~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$';

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
WHERE connector_snapshot ->> 'secret_ref' IS NOT NULL
  AND btrim(connector_snapshot ->> 'secret_ref') <> ''
  AND connector_snapshot ->> 'secret_ref' !~ '^env:[A-Za-z_][A-Za-z0-9_]*$'
  AND connector_snapshot ->> 'secret_ref' !~ '^file:/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]*$'
  AND connector_snapshot ->> 'secret_ref' !~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$';

UPDATE notification_deliveries
SET connector_snapshot = jsonb_set(
        connector_snapshot,
        '{secret_ref}',
        'null'::JSONB,
        TRUE
    )
WHERE connector_snapshot ->> 'secret_ref' IS NOT NULL
  AND btrim(connector_snapshot ->> 'secret_ref') = '';

UPDATE notification_connectors
SET secret_ref = NULL
WHERE secret_ref IS NOT NULL AND btrim(secret_ref) = '';

-- Restore values held by the version-20 compatibility table. The table is
-- persistent so callers can apply 0020 and 0021 in separate transactions.
DO $$
BEGIN
    IF to_regclass('notification_legacy_secret_refs_v20') IS NOT NULL THEN
        EXECUTE $restore$
            UPDATE notification_connectors AS c
            SET legacy_secret_ref = refs.secret_ref,
                secret_ref = NULL
            FROM notification_legacy_secret_refs_v20 AS refs
            WHERE c.id = refs.connector_id
        $restore$;
    END IF;
END
$$;

DROP TABLE IF EXISTS notification_legacy_secret_refs_v20;
