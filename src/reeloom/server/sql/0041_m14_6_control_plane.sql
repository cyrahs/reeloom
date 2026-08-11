-- M14.6 makes the current effect head explicit.  This table is not another
-- lifecycle status store: UI/control state is derived from this row plus the
-- immutable operation/result facts.
ALTER TABLE execution_operations_v2
    ADD CONSTRAINT execution_operations_v2_exact_binding
    UNIQUE (operation_id, run_id, plan_hash, operation_kind);

ALTER TABLE notification_outbox
    DROP CONSTRAINT notification_outbox_state_check,
    ADD CONSTRAINT notification_outbox_state_check CHECK (
        state IN (
            'queued', 'leased', 'retry_wait', 'sent', 'dead', 'cancelled'
        )
    );

CREATE TABLE run_lifecycle_controls_v2 (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    mode text NOT NULL CHECK (
        mode IN ('forward_v2', 'legacy_read_only')
    ),
    classification_reason text NOT NULL CHECK (
        octet_length(classification_reason) BETWEEN 1 AND 128
    ),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    effect_kind text CHECK (
        effect_kind IN ('media_move', 'subtitle_acquire')
    ),
    effect_plan_hash text,
    effect_policy text CHECK (
        effect_policy IN ('plan_only', 'manual', 'automatic')
    ),
    operation_id text UNIQUE,
    handoff_event_sequence bigint CHECK (handoff_event_sequence > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, effect_plan_hash)
        REFERENCES effect_plan_bindings_v2(run_id, plan_hash),
    FOREIGN KEY (operation_id, run_id, effect_plan_hash, effect_kind)
        REFERENCES execution_operations_v2(
            operation_id, run_id, plan_hash, operation_kind
        ),
    CHECK (
        (
            effect_kind IS NULL
            AND effect_plan_hash IS NULL
            AND effect_policy IS NULL
            AND operation_id IS NULL
            AND handoff_event_sequence IS NULL
        )
        OR (
            mode = 'forward_v2'
            AND effect_kind IS NOT NULL
            AND effect_plan_hash IS NOT NULL
            AND effect_policy IS NOT NULL
        )
    )
);

CREATE FUNCTION protect_run_lifecycle_control_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'run lifecycle control cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.run_id <> OLD.run_id
       OR NEW.mode <> OLD.mode
       OR NEW.classification_reason <> OLD.classification_reason
       OR NEW.created_at <> OLD.created_at
       OR NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'invalid lifecycle control mutation'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.operation_id IS NOT NULL AND (
        NEW.operation_id IS DISTINCT FROM OLD.operation_id
        OR NEW.effect_kind IS DISTINCT FROM OLD.effect_kind
        OR NEW.effect_plan_hash IS DISTINCT FROM OLD.effect_plan_hash
        OR NEW.effect_policy IS DISTINCT FROM OLD.effect_policy
    ) THEN
        RAISE EXCEPTION 'authorized effect head is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER run_lifecycle_controls_v2_protected
    BEFORE UPDATE OR DELETE ON run_lifecycle_controls_v2
    FOR EACH ROW EXECUTE FUNCTION protect_run_lifecycle_control_v2();

CREATE TABLE planning_terminal_results_v2 (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    plan_hash text,
    outcome text NOT NULL CHECK (
        outcome IN (
            'plan_only', 'user_failed', 'agent_failed',
            'unsupported_source', 'migration_quarantine'
        )
    ),
    reason_code text NOT NULL CHECK (
        octet_length(reason_code) BETWEEN 1 AND 128
    ),
    source_disposition text NOT NULL CHECK (
        source_disposition IN ('preserve', 'archive', 'fail')
    ),
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, plan_hash)
        REFERENCES effect_plan_bindings_v2(run_id, plan_hash)
);

CREATE TABLE generation_requests_v2 (
    request_id text PRIMARY KEY CHECK (
        octet_length(request_id) BETWEEN 1 AND 128
    ),
    request_kind text NOT NULL CHECK (
        request_kind IN (
            'operation_rescan', 'subtitle_successor', 'legacy_handoff',
            'planning_rescan'
        )
    ),
    origin_run_id text NOT NULL REFERENCES runs(run_id),
    operation_id text UNIQUE REFERENCES execution_operations_v2(operation_id),
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text NOT NULL CHECK (
        source_folder <> '' AND source_folder !~ '[/\\]'
    ),
    expected_inventory_id text,
    generation_nonce text NOT NULL UNIQUE CHECK (
        octet_length(generation_nonce) BETWEEN 1 AND 128
    ),
    lineage_key text,
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'leased', 'accepted', 'completed', 'blocked')
    ),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 20
    ),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    successor_discovery_id text UNIQUE REFERENCES discoveries(discovery_id),
    successor_run_id text UNIQUE REFERENCES runs(run_id),
    warning text CHECK (
        warning IS NULL OR octet_length(warning) BETWEEN 1 AND 128
    ),
    accepted_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'leased') =
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (state <> 'accepted' OR accepted_at IS NOT NULL),
    CHECK (
        (state = 'completed') =
        (completed_at IS NOT NULL AND successor_run_id IS NOT NULL)
    ),
    CHECK (state <> 'blocked' OR warning IS NOT NULL)
);

CREATE INDEX generation_requests_v2_claim
    ON generation_requests_v2 (available_at, created_at, request_id)
    WHERE state IN ('queued', 'leased');
CREATE UNIQUE INDEX generation_requests_v2_active_folder
    ON generation_requests_v2 (watch_id, source_folder)
    WHERE state IN ('queued', 'leased', 'accepted');

CREATE TABLE legacy_handoff_quarantines_v2 (
    quarantine_id text PRIMARY KEY,
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text NOT NULL,
    reason_code text NOT NULL CHECK (
        reason_code IN ('ambiguous_lineage', 'missing_source')
    ),
    candidate_count smallint NOT NULL CHECK (candidate_count > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (watch_id, source_folder)
);

-- Published v1/v2 subtitle effects are never resumed.  Their only active
-- consequence is a normal semantic scan carrying the already durable lineage.
-- Collapse duplicate representations of the same lineage, and quarantine
-- competing lineages for one source folder instead of guessing by timestamp.
WITH legacy_candidates AS (
    SELECT settlement.origin_run_id, successor.watch_id,
           settlement.source_folder, settlement.lineage_key
    FROM subtitle_successor_outbox AS successor
    JOIN subtitle_acquisition_settlements AS settlement
      USING (lineage_key)
    WHERE successor.state <> 'completed'
    UNION
    SELECT scan.run_id, scan.watch_id, scan.source_folder, scan.lineage_key
    FROM subtitle_scan_requests_v2 AS scan
    WHERE scan.state <> 'completed'
), unique_folders AS (
    SELECT watch_id, source_folder,
           min(origin_run_id) AS origin_run_id,
           min(lineage_key) AS lineage_key,
           count(DISTINCT lineage_key) AS lineage_count
    FROM legacy_candidates
    GROUP BY watch_id, source_folder
)
INSERT INTO generation_requests_v2
    (request_id, request_kind, origin_run_id, watch_id, source_folder,
     generation_nonce, lineage_key)
SELECT 'legacy-generation-request-v2-' || md5(
           folder.watch_id || chr(31) || folder.source_folder
       ),
       'legacy_handoff', folder.origin_run_id, folder.watch_id,
       folder.source_folder,
       'legacy-generation-v2-' || md5(
           folder.watch_id || chr(31) || folder.source_folder
           || chr(31) || folder.lineage_key
       ),
       folder.lineage_key
FROM unique_folders AS folder
WHERE folder.lineage_count = 1
  AND NOT EXISTS (
      SELECT 1 FROM generation_requests_v2 AS active
      WHERE active.watch_id = folder.watch_id
        AND active.source_folder = folder.source_folder
        AND active.state IN ('queued', 'leased', 'accepted')
  )
ON CONFLICT DO NOTHING;

WITH legacy_candidates AS (
    SELECT settlement.origin_run_id, successor.watch_id,
           settlement.source_folder, settlement.lineage_key
    FROM subtitle_successor_outbox AS successor
    JOIN subtitle_acquisition_settlements AS settlement
      USING (lineage_key)
    WHERE successor.state <> 'completed'
    UNION
    SELECT scan.run_id, scan.watch_id, scan.source_folder, scan.lineage_key
    FROM subtitle_scan_requests_v2 AS scan
    WHERE scan.state <> 'completed'
), ambiguous AS (
    SELECT watch_id, source_folder,
           count(DISTINCT lineage_key) AS lineage_count
    FROM legacy_candidates
    GROUP BY watch_id, source_folder
    HAVING count(DISTINCT lineage_key) > 1
)
INSERT INTO legacy_handoff_quarantines_v2
    (quarantine_id, watch_id, source_folder,
     reason_code, candidate_count)
SELECT 'legacy-handoff-quarantine-v2-' || md5(
           ambiguous.watch_id || chr(31) || ambiguous.source_folder
       ),
       ambiguous.watch_id, ambiguous.source_folder,
       'ambiguous_lineage', ambiguous.lineage_count
FROM ambiguous
ON CONFLICT (watch_id, source_folder) DO NOTHING;

CREATE TABLE notification_intents_v2 (
    intent_id text PRIMARY KEY CHECK (
        octet_length(intent_id) BETWEEN 1 AND 128
    ),
    run_id text NOT NULL REFERENCES runs(run_id),
    control_revision bigint NOT NULL CHECK (control_revision >= 0),
    operation_id text REFERENCES execution_operations_v2(operation_id),
    intent_kind text NOT NULL CHECK (
        intent_kind IN (
            'plan_ready', 'plan_generated', 'operation_completed',
            'attention_required', 'housekeeping_warning'
        )
    ),
    semantic_key text NOT NULL UNIQUE CHECK (
        octet_length(semantic_key) BETWEEN 1 AND 256
    ),
    state text NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'projected', 'cancelled', 'dead')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Explicitly quarantine histories with legacy effects or ambiguous multiple
-- v2 operations.  The migration only classifies database facts; it performs
-- no media-root I/O and never manufactures an approval/settlement.
WITH operation_counts AS (
    SELECT run_id, count(*) AS operation_count
    FROM execution_operations_v2
    GROUP BY run_id
), legacy_subtitle_effects AS (
    SELECT origin_run_id AS run_id
    FROM subtitle_acquisition_settlements
    UNION
    SELECT origin_run_id AS run_id
    FROM subtitle_publication_settlements_v2
)
INSERT INTO run_lifecycle_controls_v2
    (run_id, mode, classification_reason)
SELECT run.run_id,
       CASE
           WHEN legacy.run_id IS NOT NULL THEN 'legacy_read_only'
           WHEN legacy_subtitle.run_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM effect_plan_bindings_v2 AS binding
                    WHERE binding.run_id = run.run_id
                ) THEN 'legacy_read_only'
           WHEN COALESCE(operation_counts.operation_count, 0) > 1
               THEN 'legacy_read_only'
           WHEN discovery.snapshot_id LIKE 'candidate-snapshot-v2:%'
                OR COALESCE(operation_counts.operation_count, 0) = 1
                OR EXISTS (
                    SELECT 1 FROM effect_plan_bindings_v2 AS binding
                    WHERE binding.run_id = run.run_id
                )
               THEN 'forward_v2'
           ELSE 'legacy_read_only'
       END,
       CASE
           WHEN legacy.run_id IS NOT NULL THEN 'legacy_supersession'
           WHEN legacy_subtitle.run_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM effect_plan_bindings_v2 AS binding
                    WHERE binding.run_id = run.run_id
                ) THEN 'legacy_subtitle_history'
           WHEN COALESCE(operation_counts.operation_count, 0) > 1
               THEN 'ambiguous_operations'
           WHEN discovery.snapshot_id LIKE 'candidate-snapshot-v2:%'
               THEN 'semantic_discovery'
           WHEN COALESCE(operation_counts.operation_count, 0) = 1
               THEN 'single_v2_operation'
           WHEN EXISTS (
               SELECT 1 FROM effect_plan_bindings_v2 AS binding
               WHERE binding.run_id = run.run_id
           ) THEN 'v2_effect_binding'
           ELSE 'legacy_default'
       END
FROM runs AS run
JOIN discoveries AS discovery USING (discovery_id)
LEFT JOIN legacy_effect_supersessions_v2 AS legacy USING (run_id)
LEFT JOIN legacy_subtitle_effects AS legacy_subtitle USING (run_id)
LEFT JOIN operation_counts USING (run_id);

-- Prefer the exact operation binding, then the active subtitle request, then
-- the media plan head.  Ambiguous/legacy rows retain a null effect head.
WITH exact_operation AS (
    SELECT operation.run_id, operation.operation_id,
           operation.plan_hash, operation.operation_kind
    FROM execution_operations_v2 AS operation
    JOIN (
        SELECT run_id, min(operation_id) AS operation_id
        FROM execution_operations_v2
        GROUP BY run_id HAVING count(*) = 1
    ) AS single USING (run_id, operation_id)
), current_head AS (
    SELECT control.run_id,
           COALESCE(
               exact_operation.operation_kind,
               CASE WHEN subtitle.run_id IS NOT NULL
                    THEN 'subtitle_acquire' END,
               CASE WHEN media.run_id IS NOT NULL THEN 'media_move' END
           ) AS effect_kind,
           COALESCE(
               exact_operation.plan_hash,
               subtitle.plan_hash,
               media.plan_hash
           ) AS plan_hash,
           COALESCE(
               subtitle.policy,
               config.payload->>'apply_policy'
           ) AS policy,
           exact_operation.operation_id,
           state.event_sequence
    FROM run_lifecycle_controls_v2 AS control
    JOIN runs AS run USING (run_id)
    JOIN config_revisions AS config
      ON config.revision = run.config_revision
    LEFT JOIN exact_operation USING (run_id)
    LEFT JOIN subtitle_acquisition_requests AS subtitle
      ON subtitle.run_id = control.run_id
     AND (
         exact_operation.operation_id IS NULL
         OR exact_operation.operation_kind = 'subtitle_acquire'
     )
    LEFT JOIN plan_heads AS media
      ON media.run_id = control.run_id
     AND exact_operation.operation_id IS NULL
     AND subtitle.run_id IS NULL
    LEFT JOIN run_states AS state ON state.run_id = control.run_id
    WHERE control.mode = 'forward_v2'
)
UPDATE run_lifecycle_controls_v2 AS control
SET effect_kind = current_head.effect_kind,
    effect_plan_hash = current_head.plan_hash,
    effect_policy = current_head.policy,
    operation_id = current_head.operation_id,
    handoff_event_sequence = CASE
        WHEN current_head.effect_kind IS NULL THEN NULL
        ELSE current_head.event_sequence
    END,
    revision = control.revision + 1,
    updated_at = clock_timestamp()
FROM current_head
WHERE current_head.run_id = control.run_id
  AND current_head.effect_kind IS NOT NULL
  AND current_head.plan_hash IS NOT NULL
  AND current_head.policy IN ('plan_only', 'manual', 'automatic');

-- A quarantined run is read-only even when an older binary already created a
-- v2 operation for it.  Retire the database intent without touching the media
-- root.  The active claim path independently requires this exact forward-v2
-- control binding so a partially upgraded deployment cannot resurrect it.
UPDATE execution_operations_v2 AS operation
SET status = 'superseded', outcomes = '[]'::jsonb,
    lease_owner = NULL, lease_expires_at = NULL,
    updated_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
WHERE control.run_id = operation.run_id
  AND control.mode = 'legacy_read_only'
  AND operation.status IN ('authorized', 'running');

-- Adopt every actionable M14 media rescan into the canonical generation
-- ledger.  Old leases belong to the pre-cutover worker and are deliberately
-- reset.  The old notion of "completed" only meant that a scan was dispatched;
-- without a bound successor it must be generated again.
INSERT INTO generation_requests_v2
    (request_id, request_kind, origin_run_id, operation_id,
     watch_id, source_folder, expected_inventory_id, generation_nonce,
     state, attempt_count, available_at, successor_run_id, warning,
     completed_at)
SELECT 'generation-request-v2-' || md5(
           old.operation_id || chr(31) || 'm14-6-media-rescan'
       ),
       'operation_rescan', old.run_id, old.operation_id,
       discovery.watch_id, discovery.source_folder,
       discovery.inventory_id,
       'generation-v2-' || md5(
           old.operation_id || chr(31) || 'm14-6-media-rescan'
       ),
       CASE
           WHEN old.state = 'completed'
                AND old.successor_run_id IS NOT NULL THEN 'completed'
           WHEN old.state = 'blocked' THEN 'blocked'
           ELSE 'queued'
       END,
       CASE WHEN old.state = 'blocked'
            THEN LEAST(old.attempt_count, 20) ELSE 0 END,
       CASE WHEN old.state IN ('queued', 'retry_wait')
            THEN old.available_at ELSE clock_timestamp() END,
       CASE WHEN old.state = 'completed'
                  AND old.successor_run_id IS NOT NULL
            THEN old.successor_run_id ELSE NULL END,
       CASE WHEN old.state = 'blocked'
            THEN COALESCE(old.last_error, 'legacy_rescan_blocked')
            ELSE NULL END,
       CASE WHEN old.state = 'completed'
                  AND old.successor_run_id IS NOT NULL
            THEN COALESCE(old.dispatched_at, clock_timestamp())
            ELSE NULL END
FROM execution_rescan_outbox_v2 AS old
JOIN runs AS run ON run.run_id = old.run_id
JOIN discoveries AS discovery USING (discovery_id)
JOIN run_lifecycle_controls_v2 AS control
  ON control.run_id = old.run_id
 AND control.mode = 'forward_v2'
 AND control.operation_id = old.operation_id
WHERE discovery.source_folder IS NOT NULL
  AND discovery.inventory_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- M14.4 left plan-only media runs at the Agent's historical
-- awaiting-approval projection.  Make the inferred v2 head a real terminal
-- fact before the canonical projector is enabled; otherwise the UI could say
-- completed while deletion still (correctly) rejects the run.
INSERT INTO planning_terminal_results_v2
    (run_id, plan_hash, outcome, reason_code, source_disposition)
SELECT control.run_id, control.effect_plan_hash,
       'plan_only', 'plan_only_migration', 'preserve'
FROM run_lifecycle_controls_v2 AS control
WHERE control.mode = 'forward_v2'
  AND control.effect_policy = 'plan_only'
  AND control.operation_id IS NULL
ON CONFLICT (run_id) DO NOTHING;

-- Earlier semantic workers could mark a run failed and record a handled
-- inventory without creating an operation or any canonical terminal fact.
-- Preserve that current terminal truth so the lifecycle and delete command do
-- not disagree after the control-plane cutover.
INSERT INTO planning_terminal_results_v2
    (run_id, plan_hash, outcome, reason_code, source_disposition)
SELECT control.run_id, control.effect_plan_hash,
       'agent_failed', 'pre_m14_6_terminal_failure', 'preserve'
FROM run_lifecycle_controls_v2 AS control
JOIN runs AS run USING (run_id)
WHERE control.mode = 'forward_v2'
  AND control.operation_id IS NULL
  AND run.status IN ('failed', 'rolled_back')
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO handled_folder_inventories_v2
    (watch_id, source_folder, inventory_id, run_id,
     operation_id, terminal_status)
SELECT discovery.watch_id, discovery.source_folder,
       observation.inventory_id, control.run_id,
       NULL,
       CASE WHEN planning.outcome = 'plan_only'
            THEN 'completed' ELSE 'agent_failed' END
FROM run_lifecycle_controls_v2 AS control
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
JOIN watch_folder_observations AS observation
  ON observation.discovery_id = discovery.discovery_id
JOIN planning_terminal_results_v2 AS planning
  ON planning.run_id = control.run_id
WHERE control.mode = 'forward_v2'
  AND planning.outcome IN (
      'plan_only', 'agent_failed', 'migration_quarantine'
  )
  AND discovery.snapshot_id LIKE 'candidate-snapshot-v2:%'
  AND discovery.source_folder IS NOT NULL
  AND observation.inventory_id LIKE 'folder-inventory-v2:%'
ON CONFLICT DO NOTHING;

UPDATE runs AS run
SET status = 'completed'
FROM planning_terminal_results_v2 AS planning
JOIN run_lifecycle_controls_v2 AS control USING (run_id)
WHERE run.run_id = planning.run_id
  AND control.mode = 'forward_v2'
  AND planning.outcome = 'plan_only';

UPDATE run_states AS state
SET phase = 'completed', runtime_status = 'stopped',
    projection_payload = state.projection_payload
        || jsonb_build_object(
            'phase', 'completed', 'status', 'stopped',
            'stop_reason', NULL, 'failure_code', NULL
        ),
    updated_at = clock_timestamp()
FROM planning_terminal_results_v2 AS planning
JOIN run_lifecycle_controls_v2 AS control USING (run_id)
WHERE state.run_id = planning.run_id
  AND control.mode = 'forward_v2'
  AND planning.outcome = 'plan_only';

UPDATE jobs AS job
SET status = 'completed', boot_id = NULL,
    updated_at = clock_timestamp()
FROM planning_terminal_results_v2 AS planning
JOIN run_lifecycle_controls_v2 AS control USING (run_id)
WHERE job.run_id = planning.run_id
  AND control.mode = 'forward_v2'
  AND planning.outcome = 'plan_only'
  AND job.status IN ('pending', 'running');

INSERT INTO notification_intents_v2
    (intent_id, run_id, control_revision, intent_kind, semantic_key)
SELECT 'notification-intent-v2-' || md5(
           control.run_id || chr(31) || control.effect_plan_hash
           || chr(31) || 'plan_generated'
       ),
       control.run_id, control.revision, 'plan_generated',
       'plan_generated:' || control.run_id || ':'
           || control.effect_plan_hash
FROM run_lifecycle_controls_v2 AS control
JOIN planning_terminal_results_v2 AS planning USING (run_id)
WHERE control.mode = 'forward_v2'
  AND planning.outcome = 'plan_only'
ON CONFLICT (semantic_key) DO NOTHING;

-- A queued v1 plan-ready notification must not outlive the effect it
-- described.  This is a durable cancellation, not a synthetic delivery.
UPDATE notification_outbox AS notification
SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
    last_error_code = NULL, updated_at = clock_timestamp()
WHERE notification.notification_type = 'plan_ready'
  AND notification.state IN ('queued', 'leased', 'retry_wait')
  AND (
      EXISTS (
          SELECT 1
          FROM effect_plan_bindings_v2 AS binding
          JOIN run_lifecycle_controls_v2 AS control
            ON control.run_id = binding.run_id
          LEFT JOIN execution_operations_v2 AS operation
            ON operation.operation_id = control.operation_id
          LEFT JOIN planning_terminal_results_v2 AS terminal
            ON terminal.run_id = control.run_id
          WHERE notification.dedupe_key =
                'plan_ready:' || binding.plan_hash
            AND (
                control.mode = 'legacy_read_only'
                OR control.effect_policy IS DISTINCT FROM 'manual'
                OR control.effect_plan_hash IS DISTINCT FROM binding.plan_hash
                OR terminal.run_id IS NOT NULL
                OR operation.status IN (
                    'completed', 'partial', 'stale', 'collision', 'unsafe',
                    'unavailable', 'superseded'
                )
            )
      )
      OR EXISTS (
          SELECT 1
          FROM subtitle_acquisition_settlements AS settlement
          JOIN run_lifecycle_controls_v2 AS control
            ON control.run_id = settlement.origin_run_id
          WHERE settlement.origin_run_id = control.run_id
            AND notification.dedupe_key =
                'plan_ready:' || settlement.acquisition_plan_hash
      )
      OR EXISTS (
          SELECT 1
          FROM subtitle_publication_settlements_v2 AS settlement
          JOIN run_lifecycle_controls_v2 AS control
            ON control.run_id = settlement.origin_run_id
          WHERE settlement.origin_run_id = control.run_id
            AND notification.dedupe_key =
                'plan_ready:' || settlement.acquisition_plan_hash
      )
  );

-- Quarantine coordination rows only.  Approval, settlement and filesystem
-- history remain untouched, and this migration performs no media-root I/O.
DELETE FROM run_operations AS active
USING run_lifecycle_controls_v2 AS control
WHERE control.run_id = active.run_id
  AND control.mode = 'legacy_read_only';

UPDATE interactions AS interaction
SET status = 'failed',
    result = jsonb_build_object('error_code', 'legacy_effect_superseded'),
    finished_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
WHERE control.run_id = interaction.run_id
  AND control.mode = 'legacy_read_only'
  AND interaction.status = 'active';

UPDATE runs AS run
SET status = 'superseded'
FROM run_lifecycle_controls_v2 AS control
WHERE control.run_id = run.run_id
  AND control.mode = 'legacy_read_only'
  AND run.status NOT IN ('completed', 'failed', 'rolled_back', 'superseded');

UPDATE jobs AS job
SET status = 'completed', boot_id = NULL,
    updated_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
WHERE control.run_id = job.run_id
  AND control.mode = 'legacy_read_only'
  AND job.status IN ('pending', 'running');

CREATE TRIGGER planning_terminal_results_v2_immutable
    BEFORE UPDATE OR DELETE ON planning_terminal_results_v2
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON run_lifecycle_controls_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON planning_terminal_results_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON generation_requests_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON legacy_handoff_quarantines_v2 FROM PUBLIC;
REVOKE UPDATE, DELETE ON notification_intents_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT, UPDATE ON run_lifecycle_controls_v2
            TO reeloom_app;
        GRANT SELECT, INSERT ON planning_terminal_results_v2
            TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE ON generation_requests_v2
            TO reeloom_app;
        GRANT SELECT, INSERT ON legacy_handoff_quarantines_v2
            TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE ON notification_intents_v2
            TO reeloom_app;
    END IF;
END;
$$;
