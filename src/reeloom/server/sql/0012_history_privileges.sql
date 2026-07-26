DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        REVOKE UPDATE, DELETE ON
            schema_migrations,
            config_revisions,
            discoveries,
            scheduler_audit,
            agent_definitions,
            run_events,
            agent_session_batches,
            plan_lineage,
            approvals,
            approval_claims,
            approval_settlements,
            completed_layouts
        FROM reeloom_app;
    END IF;
END;
$$;
