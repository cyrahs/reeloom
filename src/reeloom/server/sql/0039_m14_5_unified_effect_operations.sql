CREATE TABLE effect_plan_bindings_v2 (
    run_id text NOT NULL REFERENCES runs(run_id),
    plan_hash text NOT NULL,
    plan_kind text NOT NULL CHECK (
        plan_kind IN ('media_move', 'subtitle_acquire')
    ),
    approval_scope text NOT NULL CHECK (
        approval_scope IN ('apply', 'subtitle_acquire')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, plan_hash),
    CHECK (
        (plan_kind = 'media_move' AND approval_scope = 'apply')
        OR
        (plan_kind = 'subtitle_acquire'
         AND approval_scope = 'subtitle_acquire')
    )
);

INSERT INTO legacy_effect_supersessions_v2
    (run_id, discovery_id, watch_id, source_folder,
     media_unsettled, folder_unsettled, subtitle_unsettled,
     fresh_scan_dispatched)
SELECT r.run_id, r.discovery_id, d.watch_id, d.source_folder,
       false, false, true, d.source_folder IS NOT NULL
FROM subtitle_acquisition_requests AS request
JOIN runs AS r ON r.run_id = request.run_id
JOIN discoveries AS d USING (discovery_id)
WHERE request.status IN ('planned', 'approved', 'blocked')
ON CONFLICT (run_id) DO NOTHING;

UPDATE runs AS run
SET status = 'superseded'
FROM legacy_effect_supersessions_v2 AS legacy
WHERE legacy.run_id = run.run_id
  AND legacy.subtitle_unsettled = true
  AND run.status NOT IN ('completed', 'rolled_back', 'superseded');

UPDATE jobs AS job
SET status = 'completed', boot_id = NULL, updated_at = clock_timestamp()
FROM legacy_effect_supersessions_v2 AS legacy
WHERE legacy.run_id = job.run_id
  AND legacy.subtitle_unsettled = true
  AND job.status IN ('pending', 'running');

UPDATE watch_folder_observations AS observation
SET discovery_id = NULL,
    status = 'settling',
    first_observed_at = clock_timestamp(),
    stable_at = NULL,
    blocked_reason = NULL,
    retry_count = 0
FROM legacy_effect_supersessions_v2 AS legacy
WHERE legacy.discovery_id = observation.discovery_id
  AND legacy.subtitle_unsettled = true;

DELETE FROM run_operations AS operation
USING legacy_effect_supersessions_v2 AS legacy
WHERE legacy.run_id = operation.run_id
  AND legacy.subtitle_unsettled = true
  AND operation.operation_kind IN (
      'subtitle_acquire', 'subtitle_recover'
  );

INSERT INTO effect_plan_bindings_v2
    (run_id, plan_hash, plan_kind, approval_scope, created_at)
SELECT run_id, plan_hash, 'media_move', 'apply', created_at
FROM plan_lineage
ON CONFLICT DO NOTHING;

CREATE FUNCTION bind_media_effect_plan_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO effect_plan_bindings_v2
        (run_id, plan_hash, plan_kind, approval_scope, created_at)
    VALUES (
        NEW.run_id, NEW.plan_hash, 'media_move', 'apply', NEW.created_at
    )
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE TRIGGER plan_lineage_effect_binding_v2
    AFTER INSERT ON plan_lineage
    FOR EACH ROW EXECUTE FUNCTION bind_media_effect_plan_v2();

ALTER TABLE approvals
    DROP CONSTRAINT approvals_scope_check,
    DROP CONSTRAINT approvals_lineage_fkey,
    ADD CONSTRAINT approvals_scope_check CHECK (
        scope IN ('apply', 'subtitle_acquire')
    ),
    ADD CONSTRAINT approvals_effect_plan_fkey
        FOREIGN KEY (run_id, plan_hash)
        REFERENCES effect_plan_bindings_v2(run_id, plan_hash);

ALTER TABLE execution_operations_v2
    DROP CONSTRAINT execution_operations_v2_run_id_plan_hash_fkey,
    ADD COLUMN operation_kind text NOT NULL DEFAULT 'media_move' CHECK (
        operation_kind IN ('media_move', 'subtitle_acquire')
    ),
    ADD CONSTRAINT execution_operations_effect_plan_fkey
        FOREIGN KEY (run_id, plan_hash)
        REFERENCES effect_plan_bindings_v2(run_id, plan_hash);

CREATE OR REPLACE FUNCTION protect_execution_operation_v2()
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
       OR NEW.operation_kind <> OLD.operation_kind
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.authorized_at <> OLD.authorized_at
       OR NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'execution operation binding is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER effect_plan_bindings_v2_immutable
    BEFORE UPDATE OR DELETE ON effect_plan_bindings_v2
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON effect_plan_bindings_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT ON effect_plan_bindings_v2 TO reeloom_app;
    END IF;
END;
$$;
