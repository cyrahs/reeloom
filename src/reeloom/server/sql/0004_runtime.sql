CREATE TABLE agent_definitions (
    definition_hash text PRIMARY KEY,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE run_states (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    phase text NOT NULL,
    runtime_status text NOT NULL,
    model_turns integer NOT NULL CHECK (model_turns >= 0),
    model_tokens bigint NOT NULL CHECK (model_tokens >= 0),
    tool_calls integer NOT NULL CHECK (tool_calls >= 0),
    failures integer NOT NULL CHECK (failures >= 0),
    plan_hash text,
    deadline_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE run_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(run_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    payload bytea NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, sequence)
);
CREATE INDEX run_events_cursor_idx ON run_events (run_id, event_id);

CREATE TABLE agent_sessions (
    session_id text PRIMARY KEY,
    run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    revision bigint NOT NULL CHECK (revision >= 0),
    items jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE agent_session_batches (
    session_id text NOT NULL REFERENCES agent_sessions(session_id),
    revision bigint NOT NULL CHECK (revision > 0),
    operation text NOT NULL CHECK (operation IN ('add', 'pop', 'clear')),
    items jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, revision)
);

CREATE TABLE plan_lineage (
    run_id text NOT NULL REFERENCES runs(run_id),
    version integer NOT NULL CHECK (version > 0),
    plan_hash text NOT NULL,
    parent_plan_hash text,
    plan_kind text NOT NULL CHECK (plan_kind IN ('initial', 'amendment')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, version),
    UNIQUE (run_id, plan_hash)
);

CREATE TABLE plan_heads (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    version integer NOT NULL CHECK (version > 0),
    plan_hash text NOT NULL,
    FOREIGN KEY (run_id, version) REFERENCES plan_lineage(run_id, version),
    UNIQUE (run_id, plan_hash)
);

REVOKE UPDATE, DELETE ON agent_definitions FROM PUBLIC;
REVOKE UPDATE, DELETE ON run_events FROM PUBLIC;
REVOKE UPDATE, DELETE ON agent_session_batches FROM PUBLIC;
REVOKE UPDATE, DELETE ON plan_lineage FROM PUBLIC;
