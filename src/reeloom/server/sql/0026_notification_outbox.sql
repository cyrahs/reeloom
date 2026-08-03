CREATE TABLE notification_outbox (
    notification_id text PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    notification_type text NOT NULL CHECK (
        notification_type IN (
            'plan_ready', 'archive_completed',
            'attention_required', 'test'
        )
    ),
    schema_version smallint NOT NULL CHECK (schema_version = 1),
    payload_json jsonb NOT NULL CHECK (
        jsonb_typeof(payload_json) = 'object'
        AND octet_length(payload_json::text) BETWEEN 2 AND 4096
    ),
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'leased', 'retry_wait', 'sent', 'dead')
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 100
    ),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    telegram_message_id bigint,
    last_error_code text CHECK (
        last_error_code IS NULL OR last_error_code IN (
            'connection', 'timeout', 'server_error', 'rate_limited',
            'client_error', 'invalid_response', 'invalid_payload',
            'lease_expired'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (octet_length(notification_id) BETWEEN 1 AND 128),
    CHECK (octet_length(dedupe_key) BETWEEN 1 AND 256),
    CHECK (lease_owner IS NULL OR octet_length(lease_owner) BETWEEN 1 AND 128),
    CHECK (
        (state = 'leased') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK ((state = 'sent') = (telegram_message_id IS NOT NULL)),
    CHECK (state <> 'sent' OR last_error_code IS NULL)
);

CREATE INDEX notification_outbox_claim
    ON notification_outbox (available_at, created_at, notification_id)
    WHERE state IN ('queued', 'retry_wait');

CREATE INDEX notification_outbox_expired_lease
    ON notification_outbox (lease_expires_at)
    WHERE state = 'leased';

REVOKE UPDATE, DELETE ON notification_outbox FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT, UPDATE ON notification_outbox TO reeloom_app;
    END IF;
END;
$$;
