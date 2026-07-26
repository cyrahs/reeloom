CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    name text NOT NULL,
    checksum character(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS service_boots (
    boot_id text PRIMARY KEY,
    process_id integer NOT NULL CHECK (process_id > 0),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    stopped_at timestamptz,
    CHECK (stopped_at IS NULL OR stopped_at >= started_at)
);

REVOKE UPDATE, DELETE ON schema_migrations FROM PUBLIC;
REVOKE UPDATE, DELETE ON service_boots FROM PUBLIC;
