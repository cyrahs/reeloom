CREATE TABLE subtitle_publication_settlements_v2 (
    lineage_key text PRIMARY KEY
        REFERENCES subtitle_acquisition_lineages(lineage_key),
    origin_run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    acquisition_plan_hash text NOT NULL UNIQUE CHECK (
        acquisition_plan_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    approval_id text NOT NULL UNIQUE,
    publication_id text NOT NULL UNIQUE CHECK (
        publication_id ~ '^subtitle-publication-v2-[0-9a-f]{64}$'
    ),
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text NOT NULL CHECK (
        source_folder <> '' AND source_folder !~ '[/\\]'
    ),
    publication_directory text NOT NULL CHECK (
        publication_directory ~ '^reeloom-acquired-[0-9a-f]{64}$'
    ),
    manifest_digest text NOT NULL CHECK (
        manifest_digest ~ '^[0-9a-f]{64}$'
    ),
    member_count smallint NOT NULL CHECK (member_count BETWEEN 1 AND 256),
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE subtitle_scan_requests_v2 (
    request_id text PRIMARY KEY CHECK (
        request_id ~ '^subtitle-scan-v2-[0-9a-f]{64}$'
    ),
    lineage_key text NOT NULL UNIQUE
        REFERENCES subtitle_publication_settlements_v2(lineage_key),
    run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text NOT NULL CHECK (
        source_folder <> '' AND source_folder !~ '[/\\]'
    ),
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN (
            'queued', 'leased', 'retry_wait', 'dispatched',
            'completed', 'blocked'
        )
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 100
    ),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    successor_discovery_id text UNIQUE REFERENCES discoveries(discovery_id),
    successor_run_id text UNIQUE REFERENCES runs(run_id),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'leased') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (state = 'completed') =
        (successor_discovery_id IS NOT NULL AND successor_run_id IS NOT NULL)
    )
);

CREATE INDEX subtitle_scan_requests_v2_claim
    ON subtitle_scan_requests_v2 (available_at, created_at, request_id)
    WHERE state IN ('queued', 'retry_wait', 'leased');

CREATE TRIGGER subtitle_publication_settlements_v2_immutable
    BEFORE UPDATE OR DELETE ON subtitle_publication_settlements_v2
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON subtitle_publication_settlements_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON subtitle_scan_requests_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT
            ON subtitle_publication_settlements_v2 TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE
            ON subtitle_scan_requests_v2 TO reeloom_app;
    END IF;
END;
$$;
