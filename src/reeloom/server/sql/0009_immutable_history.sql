CREATE FUNCTION reject_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'immutable history cannot be mutated'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER schema_migrations_immutable
    BEFORE UPDATE OR DELETE ON schema_migrations
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER config_revisions_immutable
    BEFORE UPDATE OR DELETE ON config_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER discoveries_immutable
    BEFORE UPDATE OR DELETE ON discoveries
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER scheduler_audit_immutable
    BEFORE UPDATE OR DELETE ON scheduler_audit
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER agent_definitions_immutable
    BEFORE UPDATE OR DELETE ON agent_definitions
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER run_events_immutable
    BEFORE UPDATE OR DELETE ON run_events
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER agent_session_batches_immutable
    BEFORE UPDATE OR DELETE ON agent_session_batches
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER plan_lineage_immutable
    BEFORE UPDATE OR DELETE ON plan_lineage
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER approvals_immutable
    BEFORE UPDATE OR DELETE ON approvals
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER approval_claims_immutable
    BEFORE UPDATE OR DELETE ON approval_claims
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER approval_settlements_immutable
    BEFORE UPDATE OR DELETE ON approval_settlements
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER completed_layouts_immutable
    BEFORE UPDATE OR DELETE ON completed_layouts
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
