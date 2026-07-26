CREATE TABLE api_mutations (
    mutation_id text PRIMARY KEY,
    scope text NOT NULL,
    subject_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash character(71) NOT NULL,
    status text NOT NULL CHECK (
        status IN ('active', 'completed', 'failed')
    ),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (scope, subject_id, idempotency_key)
);

CREATE INDEX api_mutations_active_idx
    ON api_mutations (status)
    WHERE status = 'active';
