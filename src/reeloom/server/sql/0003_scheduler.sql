CREATE TABLE watch_states (
    watch_id text PRIMARY KEY,
    config_revision bigint NOT NULL
        REFERENCES config_revisions(revision),
    fence bigint NOT NULL CHECK (fence > 0),
    work_type text NOT NULL CHECK (work_type IN ('anime', 'tv', 'movie')),
    settle_interval_seconds integer NOT NULL
        CHECK (settle_interval_seconds BETWEEN 1 AND 604800)
);

CREATE TABLE watch_observations (
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    relative_path text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('video', 'subtitle')),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    device bigint NOT NULL CHECK (device >= 0),
    inode bigint NOT NULL CHECK (inode >= 0),
    mtime_ns bigint NOT NULL CHECK (mtime_ns >= 0),
    ctime_ns bigint NOT NULL CHECK (ctime_ns >= 0),
    sample_digest character(64),
    first_observed_at timestamptz NOT NULL,
    stable_at timestamptz,
    PRIMARY KEY (watch_id, relative_path)
);

CREATE TABLE discoveries (
    discovery_id text PRIMARY KEY,
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    config_revision bigint NOT NULL
        REFERENCES config_revisions(revision),
    snapshot_id text NOT NULL,
    snapshot_payload jsonb NOT NULL,
    work_type text NOT NULL CHECK (work_type IN ('anime', 'tv', 'movie')),
    discovered_at timestamptz NOT NULL,
    UNIQUE (watch_id, config_revision, snapshot_id)
);
CREATE INDEX discoveries_order_idx
    ON discoveries (discovered_at DESC, discovery_id DESC);

CREATE TABLE runs (
    run_id text PRIMARY KEY,
    discovery_id text NOT NULL UNIQUE REFERENCES discoveries(discovery_id),
    config_revision bigint NOT NULL
        REFERENCES config_revisions(revision),
    work_type text NOT NULL CHECK (work_type IN ('anime', 'tv', 'movie')),
    source_capability text NOT NULL UNIQUE,
    status text NOT NULL CHECK (
        status IN ('registered', 'running', 'awaiting_approval',
                   'applying', 'completed', 'failed', 'rolled_back')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX runs_order_idx ON runs (created_at DESC, run_id DESC);

CREATE TABLE jobs (
    job_id text PRIMARY KEY,
    run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
    status text NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'failed')
    ),
    boot_id text REFERENCES service_boots(boot_id),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX jobs_claim_idx ON jobs (status, updated_at, job_id);

CREATE TABLE scheduler_audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    subject_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX scheduler_audit_once_idx
    ON scheduler_audit (event_type, subject_id);
CREATE INDEX scheduler_audit_subject_idx
    ON scheduler_audit (subject_id, audit_id);

REVOKE UPDATE, DELETE ON discoveries FROM PUBLIC;
REVOKE UPDATE, DELETE ON runs FROM PUBLIC;
REVOKE UPDATE, DELETE ON scheduler_audit FROM PUBLIC;
