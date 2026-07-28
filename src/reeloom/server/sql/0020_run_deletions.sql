ALTER TABLE run_operations
    DROP CONSTRAINT run_operations_kind,
    ADD CONSTRAINT run_operations_kind CHECK (
        operation_kind IN (
            'question',
            'revision',
            'reapply',
            'manual_apply',
            'automatic_apply',
            'recover',
            'delete'
        )
    );

CREATE TABLE run_deletions (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    deleted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TRIGGER run_deletions_immutable
    BEFORE UPDATE OR DELETE ON run_deletions
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON run_deletions FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT ON run_deletions TO reeloom_app;
    END IF;
END;
$$;
