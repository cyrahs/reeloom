ALTER TABLE run_operations
    DROP CONSTRAINT run_operations_kind,
    ADD CONSTRAINT run_operations_kind CHECK (
        operation_kind IN (
            'question', 'revision', 'reapply',
            'manual_apply', 'automatic_apply', 'recover', 'delete',
            'subtitle_acquire', 'subtitle_recover'
        )
    );

CREATE TABLE subtitle_acquisition_requests (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    plan_hash text NOT NULL UNIQUE CHECK (
        plan_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    config_revision bigint NOT NULL REFERENCES config_revisions(revision),
    policy text NOT NULL CHECK (
        policy IN ('plan_only', 'manual', 'automatic')
    ),
    status text NOT NULL CHECK (
        status IN ('planned', 'approved', 'published', 'blocked')
    ),
    approval_id text UNIQUE,
    transaction_id text UNIQUE,
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (status <> 'planned' OR approval_id IS NULL),
    CHECK (status NOT IN ('approved', 'published') OR approval_id IS NOT NULL),
    CHECK ((status = 'published') = (transaction_id IS NOT NULL)),
    CHECK ((status = 'blocked') = (failure_code IS NOT NULL))
);
CREATE INDEX subtitle_acquisition_requests_status_idx
    ON subtitle_acquisition_requests (status, updated_at, run_id);

REVOKE UPDATE, DELETE ON subtitle_acquisition_requests FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT, UPDATE
            ON subtitle_acquisition_requests TO reeloom_app;
    END IF;
END;
$$;
