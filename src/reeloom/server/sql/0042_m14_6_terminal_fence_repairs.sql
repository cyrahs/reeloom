-- Additive M14.6 repair.  0041 may already be present in a deployed database;
-- never alter its checksum to correct lifecycle facts discovered during the
-- second acceptance pass.

-- Once an exact operation is bound, approval has already been consumed.  Any
-- older plan-ready delivery is stale regardless of whether the operation is
-- authorized, running or terminal.
UPDATE notification_outbox AS notification
SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
    last_error_code = NULL, updated_at = clock_timestamp()
WHERE notification.notification_type = 'plan_ready'
  AND notification.state IN ('queued', 'leased', 'retry_wait')
  AND EXISTS (
      SELECT 1
      FROM effect_plan_bindings_v2 AS binding
      JOIN run_lifecycle_controls_v2 AS control
        ON control.run_id = binding.run_id
      WHERE control.operation_id IS NOT NULL
        AND notification.dedupe_key = 'plan_ready:' || binding.plan_hash
  );

-- Earlier failure paths could mark runs/jobs failed without writing the
-- canonical no-operation terminal fact.  Quarantine those database-only
-- remnants and record their handled inventory; this migration performs no
-- filesystem action and never fabricates an approval or operation.
INSERT INTO planning_terminal_results_v2
    (run_id, plan_hash, outcome, reason_code, source_disposition)
SELECT control.run_id, control.effect_plan_hash,
       'migration_quarantine', 'legacy_incomplete_terminal', 'preserve'
FROM run_lifecycle_controls_v2 AS control
JOIN runs AS run USING (run_id)
LEFT JOIN planning_terminal_results_v2 AS terminal USING (run_id)
WHERE control.mode = 'forward_v2'
  AND control.operation_id IS NULL
  AND terminal.run_id IS NULL
  AND run.status IN ('failed', 'rolled_back')
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO handled_folder_inventories_v2
    (watch_id, source_folder, inventory_id, run_id, terminal_status)
SELECT discovery.watch_id, discovery.source_folder, discovery.inventory_id,
       planning.run_id, 'agent_failed'
FROM planning_terminal_results_v2 AS planning
JOIN runs AS run USING (run_id)
JOIN discoveries AS discovery USING (discovery_id)
WHERE planning.outcome = 'migration_quarantine'
  AND discovery.source_folder IS NOT NULL
  AND discovery.inventory_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- 0041 intentionally preferred an already-active generation for a folder,
-- but its broad ON CONFLICT hid the media operation that lost that race.
-- Preserve a canonical blocked request so the read model can explain the
-- conflict and the user may retry after the active generation settles.
INSERT INTO generation_requests_v2
    (request_id, request_kind, origin_run_id, operation_id,
     watch_id, source_folder, expected_inventory_id, generation_nonce,
     state, warning)
SELECT 'generation-request-v2-' || md5(
           old.operation_id || chr(31) || 'm14-6-media-rescan'
       ),
       'operation_rescan', old.run_id, old.operation_id,
       discovery.watch_id, discovery.source_folder,
       discovery.inventory_id,
       'generation-v2-' || md5(
           old.operation_id || chr(31) || 'm14-6-media-rescan'
       ),
       'blocked', 'legacy_generation_conflict'
FROM execution_rescan_outbox_v2 AS old
JOIN runs AS run ON run.run_id = old.run_id
JOIN discoveries AS discovery USING (discovery_id)
JOIN run_lifecycle_controls_v2 AS control
  ON control.run_id = old.run_id
 AND control.mode = 'forward_v2'
 AND control.operation_id = old.operation_id
LEFT JOIN generation_requests_v2 AS current
  ON current.operation_id = old.operation_id
WHERE current.request_id IS NULL
  AND discovery.source_folder IS NOT NULL
  AND discovery.inventory_id IS NOT NULL
ON CONFLICT (operation_id) DO NOTHING;
