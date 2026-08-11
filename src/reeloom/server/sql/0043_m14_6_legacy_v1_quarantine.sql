-- Additive M14.6 repair.  0039 created effect bindings for every historical
-- media plan, so 0041 could mistake a candidate-snapshot-v1 run for an active
-- forward-v2 effect.  Snapshot-v1 effects are permanently read-only: retain
-- their audit rows, cancel active control intents and perform no filesystem
-- action.

UPDATE notification_outbox AS notification
SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
    last_error_code = NULL, updated_at = clock_timestamp()
FROM effect_plan_bindings_v2 AS binding
JOIN run_lifecycle_controls_v2 AS control
  ON control.run_id = binding.run_id
JOIN runs AS run ON run.run_id = control.run_id
JOIN discoveries AS discovery USING (discovery_id)
WHERE control.mode = 'forward_v2'
  AND discovery.snapshot_id NOT LIKE 'candidate-snapshot-v2:%'
  AND notification.notification_type = 'plan_ready'
  AND notification.state IN ('queued', 'leased', 'retry_wait')
  AND notification.dedupe_key = 'plan_ready:' || binding.plan_hash;

UPDATE execution_operations_v2 AS operation
SET status = 'superseded', outcomes = '[]'::jsonb,
    lease_owner = NULL, lease_expires_at = NULL,
    updated_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
WHERE operation.run_id = control.run_id
  AND control.mode = 'forward_v2'
  AND discovery.snapshot_id NOT LIKE 'candidate-snapshot-v2:%'
  AND operation.status IN ('authorized', 'running');

DELETE FROM run_operations AS coordination
USING run_lifecycle_controls_v2 AS control,
      runs AS run,
      discoveries AS discovery
WHERE coordination.run_id = control.run_id
  AND run.run_id = control.run_id
  AND discovery.discovery_id = run.discovery_id
  AND control.mode = 'forward_v2'
  AND discovery.snapshot_id NOT LIKE 'candidate-snapshot-v2:%';

-- An older writer could claim the exact approval and crash before creating a
-- v2 operation.  The immutable claim is audit history, not a permanent lock:
-- terminate the abandoned planning effect, preserve the source and request
-- one fresh semantic generation.  No filesystem effect is resumed here.
INSERT INTO planning_terminal_results_v2
    (run_id, plan_hash, outcome, reason_code, source_disposition)
SELECT control.run_id, control.effect_plan_hash,
       'migration_quarantine', 'orphan_approval_claim', 'preserve'
FROM run_lifecycle_controls_v2 AS control
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
JOIN approval_claims AS claim
  ON claim.run_id = control.run_id
 AND claim.plan_hash = control.effect_plan_hash
LEFT JOIN approval_settlements AS settlement
  ON settlement.approval_id = claim.approval_id
LEFT JOIN execution_operations_v2 AS operation
  ON operation.approval_id = claim.approval_id
LEFT JOIN planning_terminal_results_v2 AS terminal
  ON terminal.run_id = control.run_id
WHERE control.mode = 'forward_v2'
  AND control.operation_id IS NULL
  AND control.effect_plan_hash IS NOT NULL
  AND terminal.run_id IS NULL
  AND settlement.approval_id IS NULL
  AND operation.operation_id IS NULL
  AND discovery.snapshot_id LIKE 'candidate-snapshot-v2:%'
  AND run.status IN (
      'registered', 'running', 'awaiting_approval', 'applying'
  )
ON CONFLICT (run_id) DO NOTHING;

UPDATE notification_outbox AS notification
SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
    last_error_code = NULL, updated_at = clock_timestamp()
FROM planning_terminal_results_v2 AS terminal,
     run_lifecycle_controls_v2 AS control
WHERE terminal.run_id = control.run_id
  AND terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim'
  AND notification.notification_type = 'plan_ready'
  AND notification.state IN ('queued', 'leased', 'retry_wait')
  AND notification.dedupe_key = 'plan_ready:' || terminal.plan_hash;

INSERT INTO handled_folder_inventories_v2
    (watch_id, source_folder, inventory_id, run_id, terminal_status)
SELECT discovery.watch_id, discovery.source_folder, discovery.inventory_id,
       terminal.run_id, 'agent_failed'
FROM planning_terminal_results_v2 AS terminal
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
WHERE terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim'
  AND discovery.source_folder IS NOT NULL
  AND discovery.inventory_id LIKE 'folder-inventory-v2:%'
ON CONFLICT DO NOTHING;

INSERT INTO generation_requests_v2
    (request_id, request_kind, origin_run_id, watch_id, source_folder,
     expected_inventory_id, generation_nonce)
SELECT 'planning-generation-request-v2-' || md5(terminal.run_id),
       'planning_rescan', terminal.run_id, discovery.watch_id,
       discovery.source_folder, discovery.inventory_id,
       'planning-generation-v2-' || md5(
           terminal.run_id || chr(31) || discovery.inventory_id
       )
FROM planning_terminal_results_v2 AS terminal
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
WHERE terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim'
  AND discovery.source_folder IS NOT NULL
  AND discovery.inventory_id LIKE 'folder-inventory-v2:%'
ON CONFLICT DO NOTHING;

UPDATE run_states AS state
SET phase = 'failed', runtime_status = 'failed',
    projection_payload = state.projection_payload
        || jsonb_build_object(
            'phase', 'failed', 'status', 'failed',
            'pending_tool_calls', '[]'::jsonb,
            'observed_tool_calls', '[]'::jsonb,
            'stop_reason', 'fatal_error',
            'failure_code', 'orphan_approval_claim'
        ),
    updated_at = clock_timestamp()
FROM planning_terminal_results_v2 AS terminal
WHERE state.run_id = terminal.run_id
  AND terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim';

UPDATE jobs AS job
SET status = 'completed', boot_id = NULL, updated_at = clock_timestamp()
FROM planning_terminal_results_v2 AS terminal
WHERE job.run_id = terminal.run_id
  AND terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim'
  AND job.status IN ('pending', 'running');

UPDATE runs AS run
SET status = 'failed'
FROM planning_terminal_results_v2 AS terminal
WHERE run.run_id = terminal.run_id
  AND terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim'
  AND run.status NOT IN ('completed', 'superseded');

INSERT INTO notification_intents_v2
    (intent_id, run_id, control_revision, intent_kind, semantic_key)
SELECT 'notification-intent-v2-' || md5(
           terminal.run_id || chr(31) || 'orphan-approval-claim'
       ),
       terminal.run_id, control.revision, 'attention_required',
       'attention_required:orphan_approval_claim:' || terminal.run_id
FROM planning_terminal_results_v2 AS terminal
JOIN run_lifecycle_controls_v2 AS control USING (run_id)
WHERE terminal.outcome = 'migration_quarantine'
  AND terminal.reason_code = 'orphan_approval_claim'
ON CONFLICT (semantic_key) DO NOTHING;

-- Effect coordination moved to execution_operations_v2.  A semantic-v2 run
-- can still carry one of these rows when an older binary stopped between
-- reserving a v1 effect and writing its settlement.  Production no longer
-- has a consumer for it, while deletion and the lifecycle projector both
-- treat it as active forever.  Remove only retired effect kinds; live Agent
-- interactions and delete reservations remain untouched.
DELETE FROM run_operations AS coordination
USING run_lifecycle_controls_v2 AS control
WHERE coordination.run_id = control.run_id
  AND coordination.operation_kind IN (
      'manual_apply', 'automatic_apply', 'recover',
      'subtitle_acquire', 'subtitle_recover'
  );

-- The active subtitle reconciler must never keep issuing approvals for a
-- quarantined v1 request.  Preserve the request as readable history but make
-- its non-operational state explicit and terminal.
UPDATE subtitle_acquisition_requests AS request
SET status = 'blocked', failure_code = 'legacy_read_only',
    failure_diagnostic = NULL, updated_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
WHERE control.run_id = request.run_id
  AND control.mode = 'legacy_read_only'
  AND request.status IN ('planned', 'approved');

UPDATE jobs AS job
SET status = 'completed', boot_id = NULL, updated_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
WHERE job.run_id = control.run_id
  AND control.mode = 'forward_v2'
  AND discovery.snapshot_id NOT LIKE 'candidate-snapshot-v2:%'
  AND job.status IN ('pending', 'running');

UPDATE runs AS run
SET status = 'superseded'
FROM run_lifecycle_controls_v2 AS control,
     discoveries AS discovery
WHERE control.run_id = run.run_id
  AND discovery.discovery_id = run.discovery_id
  AND control.mode = 'forward_v2'
  AND discovery.snapshot_id NOT LIKE 'candidate-snapshot-v2:%'
  AND run.status NOT IN ('completed', 'failed', 'rolled_back', 'superseded');

-- The protection trigger intentionally makes the effect mode immutable during
-- normal operation.  This one-time migration is the only permitted repair of
-- the incorrect 0041 classification.
ALTER TABLE run_lifecycle_controls_v2
    DISABLE TRIGGER run_lifecycle_controls_v2_protected;

UPDATE run_lifecycle_controls_v2 AS control
SET mode = 'legacy_read_only',
    classification_reason = 'legacy_v1_snapshot',
    effect_kind = NULL, effect_plan_hash = NULL, effect_policy = NULL,
    operation_id = NULL, handoff_event_sequence = NULL,
    revision = control.revision + 1,
    updated_at = clock_timestamp()
FROM runs AS run
JOIN discoveries AS discovery USING (discovery_id)
WHERE control.run_id = run.run_id
  AND control.mode = 'forward_v2'
  AND discovery.snapshot_id NOT LIKE 'candidate-snapshot-v2:%';

ALTER TABLE run_lifecycle_controls_v2
    ENABLE TRIGGER run_lifecycle_controls_v2_protected;
