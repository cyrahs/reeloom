CREATE TABLE handled_folder_inventories_v2 (
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text NOT NULL CHECK (
        source_folder <> '' AND source_folder !~ '[/\\]'
    ),
    inventory_id text NOT NULL CHECK (
        inventory_id ~ '^folder-inventory-v2:[0-9a-f]{64}$'
    ),
    run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    operation_id text UNIQUE REFERENCES execution_operations_v2(operation_id),
    terminal_status text NOT NULL CHECK (
        terminal_status IN (
            'completed', 'partial', 'stale', 'collision', 'unsafe',
            'unavailable', 'agent_failed'
        )
    ),
    handled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (watch_id, source_folder, inventory_id)
);

CREATE TABLE folder_housekeeping_v2 (
    housekeeping_id text PRIMARY KEY CHECK (
        housekeeping_id ~ '^folder-housekeeping-v2-[0-9a-f]{64}$'
    ),
    run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    operation_id text UNIQUE REFERENCES execution_operations_v2(operation_id),
    config_revision bigint NOT NULL REFERENCES config_revisions(revision),
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text NOT NULL CHECK (
        source_folder <> '' AND source_folder !~ '[/\\]'
    ),
    target_folder text NOT NULL CHECK (
        target_folder <> '' AND target_folder !~ '[/\\]'
    ),
    action text NOT NULL CHECK (action IN ('archive', 'fail')),
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN (
            'queued', 'leased', 'retry_wait', 'completed', 'warning'
        )
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 20
    ),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    warning text CHECK (
        warning IS NULL OR octet_length(warning) BETWEEN 1 AND 128
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'leased') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK ((state = 'warning') = (warning IS NOT NULL))
);

CREATE INDEX folder_housekeeping_v2_claim
    ON folder_housekeeping_v2 (available_at, created_at, housekeeping_id)
    WHERE state IN ('queued', 'retry_wait', 'leased');

CREATE TRIGGER handled_folder_inventories_v2_immutable
    BEFORE UPDATE OR DELETE ON handled_folder_inventories_v2
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON handled_folder_inventories_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON folder_housekeeping_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT
            ON handled_folder_inventories_v2 TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE
            ON folder_housekeeping_v2 TO reeloom_app;
    END IF;
END;
$$;
