ALTER TABLE discoveries
    ADD COLUMN source_folder text,
    ADD COLUMN folder_generation_id text,
    ADD COLUMN inventory_id text,
    ADD CONSTRAINT discoveries_folder_fields_check CHECK (
        (
            source_folder IS NULL
            AND folder_generation_id IS NULL
            AND inventory_id IS NULL
        )
        OR (
            source_folder IS NOT NULL
            AND source_folder <> ''
            AND source_folder !~ '[/\\]'
            AND folder_generation_id IS NOT NULL
            AND inventory_id IS NOT NULL
        )
    );

ALTER TABLE discoveries
    DROP CONSTRAINT discoveries_watch_id_config_revision_snapshot_id_key;

CREATE UNIQUE INDEX discoveries_legacy_snapshot_key
    ON discoveries (watch_id, config_revision, snapshot_id)
    WHERE folder_generation_id IS NULL;
CREATE UNIQUE INDEX discoveries_folder_generation_key
    ON discoveries (folder_generation_id)
    WHERE folder_generation_id IS NOT NULL;

CREATE TABLE watch_folder_observations (
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    folder_name text NOT NULL CHECK (
        folder_name <> '' AND folder_name !~ '[/\\]'
    ),
    config_revision bigint NOT NULL
        REFERENCES config_revisions(revision),
    folder_device bigint CHECK (folder_device >= 0),
    folder_inode bigint CHECK (folder_inode >= 0),
    inventory_id text,
    inventory_payload jsonb,
    snapshot_id text,
    snapshot_payload jsonb,
    first_observed_at timestamptz NOT NULL,
    stable_at timestamptz,
    discovery_id text UNIQUE REFERENCES discoveries(discovery_id),
    status text NOT NULL CHECK (
        status IN ('settling', 'active', 'blocked', 'settled')
    ),
    blocked_reason text,
    PRIMARY KEY (watch_id, folder_name),
    CHECK (
        (
            status = 'blocked'
            AND blocked_reason IS NOT NULL
        )
        OR (
            status <> 'blocked'
            AND blocked_reason IS NULL
            AND folder_device IS NOT NULL
            AND folder_inode IS NOT NULL
            AND inventory_id IS NOT NULL
            AND inventory_payload IS NOT NULL
            AND snapshot_id IS NOT NULL
            AND snapshot_payload IS NOT NULL
        )
    )
);
CREATE INDEX watch_folder_observations_status_idx
    ON watch_folder_observations (watch_id, status, folder_name);

CREATE TABLE folder_disposition_plans (
    plan_hash text PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(run_id),
    media_plan_hash text,
    folder_generation_id text NOT NULL,
    action text NOT NULL CHECK (
        action IN ('archive', 'fail', 'remove_empty')
    ),
    target_relative text,
    source_root_device bigint NOT NULL CHECK (source_root_device >= 0),
    source_root_inode bigint NOT NULL CHECK (source_root_inode >= 0),
    target_name_key text,
    inventory_id text NOT NULL,
    file_count integer NOT NULL CHECK (file_count >= 0),
    reason_code text NOT NULL,
    canonical_record bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, plan_hash),
    FOREIGN KEY (run_id, media_plan_hash)
        REFERENCES plan_lineage(run_id, plan_hash),
    CHECK (
        (
            action = 'remove_empty'
            AND target_relative IS NULL
            AND target_name_key IS NULL
        )
        OR (
            action <> 'remove_empty'
            AND target_relative IS NOT NULL
            AND target_name_key IS NOT NULL
        )
    )
);
CREATE UNIQUE INDEX folder_disposition_target_reservation
    ON folder_disposition_plans (
        source_root_device, source_root_inode, action, target_name_key
    )
    WHERE target_name_key IS NOT NULL;
CREATE INDEX folder_disposition_media_idx
    ON folder_disposition_plans (run_id, media_plan_hash)
    WHERE media_plan_hash IS NOT NULL;
CREATE UNIQUE INDEX folder_disposition_media_inventory_once
    ON folder_disposition_plans
        (run_id, media_plan_hash, inventory_id, target_relative)
    NULLS NOT DISTINCT
    WHERE media_plan_hash IS NOT NULL;
CREATE UNIQUE INDEX folder_disposition_failure_inventory_once
    ON folder_disposition_plans
        (run_id, reason_code, inventory_id, target_relative)
    WHERE media_plan_hash IS NULL;

CREATE TABLE folder_disposition_approvals (
    approval_id text PRIMARY KEY,
    run_id text NOT NULL,
    plan_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    canonical_record bytea NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, plan_hash)
        REFERENCES folder_disposition_plans(run_id, plan_hash)
);

CREATE TABLE folder_disposition_claims (
    approval_id text PRIMARY KEY
        REFERENCES folder_disposition_approvals(approval_id),
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE folder_disposition_transactions (
    transaction_id text PRIMARY KEY,
    approval_id text NOT NULL UNIQUE
        REFERENCES folder_disposition_claims(approval_id),
    status text NOT NULL CHECK (
        status IN (
            'prepared', 'renamed', 'completed',
            'blocked', 'recovery_required'
        )
    ),
    source_device bigint NOT NULL CHECK (source_device >= 0),
    source_inode bigint NOT NULL CHECK (source_inode >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE folder_disposition_settlements (
    approval_id text PRIMARY KEY
        REFERENCES folder_disposition_claims(approval_id),
    transaction_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status = 'completed'),
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (transaction_id)
        REFERENCES folder_disposition_transactions(transaction_id)
);

CREATE TRIGGER folder_disposition_plans_immutable
    BEFORE UPDATE OR DELETE ON folder_disposition_plans
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER folder_disposition_approvals_immutable
    BEFORE UPDATE OR DELETE ON folder_disposition_approvals
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER folder_disposition_claims_immutable
    BEFORE UPDATE OR DELETE ON folder_disposition_claims
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER folder_disposition_settlements_immutable
    BEFORE UPDATE OR DELETE ON folder_disposition_settlements
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON folder_disposition_plans FROM PUBLIC;
REVOKE UPDATE, DELETE ON folder_disposition_approvals FROM PUBLIC;
REVOKE UPDATE, DELETE ON folder_disposition_claims FROM PUBLIC;
REVOKE UPDATE, DELETE ON folder_disposition_settlements FROM PUBLIC;
