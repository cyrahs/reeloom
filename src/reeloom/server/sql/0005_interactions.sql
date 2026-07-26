CREATE TABLE run_operations (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    operation_id text NOT NULL UNIQUE,
    operation_kind text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE interactions (
    interaction_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(run_id),
    kind text NOT NULL CHECK (kind IN ('question', 'revision', 'reapply')),
    idempotency_key text NOT NULL,
    request_hash character(71) NOT NULL,
    expected_plan_hash text NOT NULL,
    session_revision bigint NOT NULL CHECK (session_revision >= 0),
    status text NOT NULL CHECK (
        status IN ('active', 'completed', 'failed')
    ),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (run_id, idempotency_key)
);
CREATE INDEX interactions_run_idx
    ON interactions (run_id, created_at DESC, interaction_id DESC);

REVOKE DELETE ON interactions FROM PUBLIC;
