CREATE TABLE approvals (
    approval_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(run_id),
    plan_hash text NOT NULL,
    scope text NOT NULL CHECK (scope = 'apply'),
    expires_at timestamptz NOT NULL,
    canonical_record bytea NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, plan_hash)
);

CREATE TABLE approval_claims (
    approval_id text PRIMARY KEY REFERENCES approvals(approval_id),
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE approval_settlements (
    approval_id text PRIMARY KEY REFERENCES approval_claims(approval_id),
    transaction_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('completed', 'rolled_back')),
    applied_count integer NOT NULL CHECK (applied_count >= 0),
    rolled_back_count integer NOT NULL CHECK (rolled_back_count >= 0),
    failure_code text,
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE UPDATE, DELETE ON approvals FROM PUBLIC;
REVOKE UPDATE, DELETE ON approval_claims FROM PUBLIC;
REVOKE UPDATE, DELETE ON approval_settlements FROM PUBLIC;
