CREATE TABLE completed_layouts (
    run_id text NOT NULL REFERENCES runs(run_id),
    version integer NOT NULL CHECK (version > 0),
    plan_hash text NOT NULL,
    transaction_id text NOT NULL UNIQUE,
    layout_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, version),
    UNIQUE (run_id, plan_hash)
);

CREATE TABLE completed_layout_heads (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    version integer NOT NULL CHECK (version > 0),
    plan_hash text NOT NULL,
    FOREIGN KEY (run_id, version)
        REFERENCES completed_layouts(run_id, version)
);

REVOKE UPDATE, DELETE ON completed_layouts FROM PUBLIC;
