CREATE TABLE execution_operations_v2 (
    operation_id text PRIMARY KEY,
    schema_version smallint NOT NULL CHECK (schema_version = 2),
    run_id text NOT NULL,
    plan_hash text NOT NULL,
    approval_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (
        status IN (
            'authorized', 'running', 'completed', 'partial', 'stale',
            'collision', 'unsafe', 'unavailable', 'superseded'
        )
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 100
    ),
    outcomes jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(outcomes) = 'array'
        AND jsonb_array_length(outcomes) <= 10000
        AND octet_length(outcomes::text) <= 131072
    ),
    lease_owner text,
    lease_expires_at timestamptz,
    authorized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, plan_hash),
    FOREIGN KEY (run_id, plan_hash)
        REFERENCES plan_lineage(run_id, plan_hash),
    FOREIGN KEY (approval_id, run_id, plan_hash)
        REFERENCES approvals(approval_id, run_id, plan_hash),
    CHECK (octet_length(operation_id) BETWEEN 1 AND 128),
    CHECK (octet_length(lease_owner) BETWEEN 1 AND 128),
    CHECK (
        (status = 'running') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (status IN (
            'completed', 'partial', 'stale', 'collision', 'unsafe',
            'unavailable'
        )) = (jsonb_array_length(outcomes) > 0)
    ),
    CHECK (
        status <> 'authorized'
        OR (attempt_count = 0 AND jsonb_array_length(outcomes) = 0)
    ),
    CHECK (
        status <> 'superseded' OR jsonb_array_length(outcomes) = 0
    )
);

CREATE INDEX execution_operations_v2_claim
    ON execution_operations_v2 (
        status, lease_expires_at, authorized_at, operation_id
    )
    WHERE status IN ('authorized', 'running');

CREATE FUNCTION protect_execution_operation_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'execution operation cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status NOT IN ('authorized', 'running') THEN
        RAISE EXCEPTION 'terminal execution operation is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.operation_id <> OLD.operation_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.plan_hash <> OLD.plan_hash
       OR NEW.approval_id <> OLD.approval_id
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.authorized_at <> OLD.authorized_at
       OR NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'execution operation binding is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_operations_v2_protected
    BEFORE UPDATE OR DELETE ON execution_operations_v2
    FOR EACH ROW EXECUTE FUNCTION protect_execution_operation_v2();

REVOKE UPDATE, DELETE ON execution_operations_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT, UPDATE
            ON execution_operations_v2 TO reeloom_app;
    END IF;
END;
$$;
