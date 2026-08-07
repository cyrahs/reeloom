CREATE TABLE execution_operation_results_v2 (
    operation_id text PRIMARY KEY
        REFERENCES execution_operations_v2(operation_id),
    items jsonb NOT NULL CHECK (
        jsonb_typeof(items) = 'array'
        AND jsonb_array_length(items) BETWEEN 1 AND 10000
        AND octet_length(items::text) <= 1048576
    ),
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(warnings) = 'array'
        AND jsonb_array_length(warnings) <= 1000
        AND octet_length(warnings::text) <= 131072
    ),
    fresh_scan_required boolean NOT NULL,
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE execution_rescan_outbox_v2 (
    operation_id text PRIMARY KEY
        REFERENCES execution_operation_results_v2(operation_id),
    run_id text NOT NULL REFERENCES runs(run_id),
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'leased', 'retry_wait', 'completed', 'blocked')
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 100
    ),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    dispatched_at timestamptz,
    successor_run_id text REFERENCES runs(run_id),
    last_error text CHECK (
        last_error IS NULL OR octet_length(last_error) BETWEEN 1 AND 128
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id),
    CHECK (
        (state = 'leased') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (state IN ('completed', 'blocked')) = (dispatched_at IS NOT NULL)
    )
);

CREATE INDEX execution_rescan_outbox_v2_claim
    ON execution_rescan_outbox_v2 (
        available_at, created_at, operation_id
    )
    WHERE state IN ('queued', 'retry_wait');

CREATE TRIGGER execution_operation_results_v2_immutable
    BEFORE UPDATE OR DELETE ON execution_operation_results_v2
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON execution_operation_results_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON execution_rescan_outbox_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT
            ON execution_operation_results_v2 TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE
            ON execution_rescan_outbox_v2 TO reeloom_app;
    END IF;
END;
$$;
