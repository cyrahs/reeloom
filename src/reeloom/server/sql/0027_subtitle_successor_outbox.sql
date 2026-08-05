ALTER TABLE runs
    DROP CONSTRAINT runs_status_check,
    ADD CONSTRAINT runs_status_check CHECK (
        status IN (
            'registered', 'running', 'awaiting_approval', 'applying',
            'completed', 'failed', 'rolled_back', 'superseded'
        )
    );

CREATE TABLE subtitle_acquisition_lineages (
    lineage_key text PRIMARY KEY CHECK (
        lineage_key ~ '^subtitle-lineage-v1-[0-9a-f]{64}$'
    ),
    root_discovery_id text NOT NULL UNIQUE
        REFERENCES discoveries(discovery_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE runs
    ADD COLUMN subtitle_acquisition_lineage_key text
        REFERENCES subtitle_acquisition_lineages(lineage_key);
CREATE INDEX runs_subtitle_acquisition_lineage_idx
    ON runs (subtitle_acquisition_lineage_key)
    WHERE subtitle_acquisition_lineage_key IS NOT NULL;

CREATE TABLE subtitle_acquisition_settlements (
    lineage_key text PRIMARY KEY
        REFERENCES subtitle_acquisition_lineages(lineage_key),
    origin_run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    acquisition_plan_hash text NOT NULL UNIQUE CHECK (
        acquisition_plan_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    approval_id text NOT NULL UNIQUE,
    transaction_id text NOT NULL UNIQUE,
    source_folder text NOT NULL CHECK (
        source_folder <> '' AND source_folder !~ '[/\\]'
    ),
    source_folder_device bigint NOT NULL CHECK (source_folder_device >= 0),
    source_folder_inode bigint NOT NULL CHECK (source_folder_inode >= 0),
    original_snapshot_id text NOT NULL,
    destination_name text NOT NULL CHECK (
        destination_name ~ '^reeloom-acquired-[0-9a-f]{64}$'
    ),
    destination_device bigint NOT NULL CHECK (destination_device >= 0),
    destination_inode bigint NOT NULL CHECK (destination_inode >= 0),
    member_manifest_json jsonb NOT NULL CHECK (
        jsonb_typeof(member_manifest_json) = 'array'
        AND jsonb_array_length(member_manifest_json) BETWEEN 1 AND 256
        AND octet_length(member_manifest_json::text) <= 131072
    ),
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE subtitle_successor_outbox (
    lineage_key text PRIMARY KEY
        REFERENCES subtitle_acquisition_settlements(lineage_key),
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    config_revision bigint NOT NULL REFERENCES config_revisions(revision),
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'leased', 'retry_wait', 'completed', 'blocked')
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 100
    ),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    successor_discovery_id text UNIQUE REFERENCES discoveries(discovery_id),
    successor_run_id text UNIQUE REFERENCES runs(run_id),
    fresh_snapshot_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'leased') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (state = 'completed') =
        (
            successor_discovery_id IS NOT NULL
            AND successor_run_id IS NOT NULL
            AND fresh_snapshot_id IS NOT NULL
        )
    )
);
CREATE INDEX subtitle_successor_outbox_claim_idx
    ON subtitle_successor_outbox
        (available_at, created_at, lineage_key)
    WHERE state IN ('queued', 'retry_wait');
CREATE INDEX subtitle_successor_outbox_lease_idx
    ON subtitle_successor_outbox (lease_expires_at)
    WHERE state = 'leased';

CREATE TRIGGER subtitle_acquisition_lineages_immutable
    BEFORE UPDATE OR DELETE ON subtitle_acquisition_lineages
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER subtitle_acquisition_settlements_immutable
    BEFORE UPDATE OR DELETE ON subtitle_acquisition_settlements
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON subtitle_acquisition_lineages FROM PUBLIC;
REVOKE UPDATE, DELETE ON subtitle_acquisition_settlements FROM PUBLIC;
REVOKE UPDATE, DELETE ON subtitle_successor_outbox FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT ON subtitle_acquisition_lineages TO reeloom_app;
        GRANT SELECT, INSERT ON subtitle_acquisition_settlements TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE ON subtitle_successor_outbox TO reeloom_app;
    END IF;
END;
$$;
