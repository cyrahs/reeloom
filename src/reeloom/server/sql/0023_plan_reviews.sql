CREATE TABLE plan_reviews (
    run_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    plan_hash text NOT NULL,
    schema_version text NOT NULL CHECK (
        schema_version = 'plan-review-v1'
    ),
    -- JSONB text adds separators to the 64 KiB canonical payload.
    payload jsonb NOT NULL CHECK (
        octet_length(payload::text) <= 69632
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, version),
    FOREIGN KEY (run_id, version, plan_hash)
        REFERENCES plan_lineage(run_id, version, plan_hash)
);

REVOKE UPDATE, DELETE ON plan_reviews FROM PUBLIC;

CREATE TRIGGER plan_reviews_immutable
    BEFORE UPDATE OR DELETE ON plan_reviews
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
