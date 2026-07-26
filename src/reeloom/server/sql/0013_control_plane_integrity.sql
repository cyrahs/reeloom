ALTER TABLE plan_lineage
    ADD CONSTRAINT plan_lineage_version_hash_key
    UNIQUE (run_id, version, plan_hash);

ALTER TABLE plan_heads
    DROP CONSTRAINT plan_heads_run_id_version_fkey,
    ADD CONSTRAINT plan_heads_lineage_fkey
        FOREIGN KEY (run_id, version, plan_hash)
        REFERENCES plan_lineage(run_id, version, plan_hash);

ALTER TABLE approvals
    ADD CONSTRAINT approvals_lineage_fkey
        FOREIGN KEY (run_id, plan_hash)
        REFERENCES plan_lineage(run_id, plan_hash);

ALTER TABLE completed_layouts
    ADD CONSTRAINT completed_layouts_lineage_fkey
        FOREIGN KEY (run_id, plan_hash)
        REFERENCES plan_lineage(run_id, plan_hash),
    ADD CONSTRAINT completed_layouts_version_hash_key
        UNIQUE (run_id, version, plan_hash);

ALTER TABLE completed_layout_heads
    DROP CONSTRAINT completed_layout_heads_run_id_version_fkey,
    ADD CONSTRAINT completed_layout_heads_layout_fkey
        FOREIGN KEY (run_id, version, plan_hash)
        REFERENCES completed_layouts(run_id, version, plan_hash);

CREATE FUNCTION reject_terminal_status_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'active' THEN
        RAISE EXCEPTION 'terminal record cannot be mutated'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER interactions_terminal_immutable
    BEFORE UPDATE ON interactions
    FOR EACH ROW EXECUTE FUNCTION reject_terminal_status_mutation();
CREATE TRIGGER api_mutations_terminal_immutable
    BEFORE UPDATE ON api_mutations
    FOR EACH ROW EXECUTE FUNCTION reject_terminal_status_mutation();

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        REVOKE INSERT ON schema_migrations FROM reeloom_app;
        REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM reeloom_app;
        GRANT DELETE ON watch_observations, run_operations
            TO reeloom_app;
    END IF;
END;
$$;
