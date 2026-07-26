CREATE TABLE config_revisions (
    revision_id text PRIMARY KEY,
    revision bigint NOT NULL UNIQUE CHECK (revision > 0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE config_heads (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL UNIQUE
        REFERENCES config_revisions(revision)
);

REVOKE UPDATE, DELETE ON config_revisions FROM PUBLIC;
REVOKE DELETE ON config_heads FROM PUBLIC;
