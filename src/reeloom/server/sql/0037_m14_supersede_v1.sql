CREATE TABLE legacy_effect_supersessions_v2 (
    run_id text PRIMARY KEY REFERENCES runs(run_id),
    discovery_id text NOT NULL REFERENCES discoveries(discovery_id),
    watch_id text NOT NULL REFERENCES watch_states(watch_id),
    source_folder text,
    media_unsettled boolean NOT NULL,
    folder_unsettled boolean NOT NULL,
    subtitle_unsettled boolean NOT NULL,
    fresh_scan_dispatched boolean NOT NULL DEFAULT true,
    superseded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        media_unsettled OR folder_unsettled OR subtitle_unsettled
    ),
    CHECK (
        source_folder IS NULL
        OR (source_folder <> '' AND source_folder !~ '[/\\]')
    )
);

WITH affected AS (
    SELECT r.run_id, r.discovery_id, d.watch_id, d.source_folder,
           EXISTS (
               SELECT 1
               FROM approval_claims AS claim
               LEFT JOIN approval_settlements AS settlement
                 ON settlement.approval_id = claim.approval_id
               WHERE claim.run_id = r.run_id
                 AND settlement.approval_id IS NULL
           ) OR (
               d.snapshot_id NOT LIKE 'candidate-snapshot-v2:%'
               AND r.status NOT IN (
                   'completed', 'rolled_back', 'superseded'
               )
           ) AS media_unsettled,
           EXISTS (
               SELECT 1
               FROM folder_disposition_approvals AS approval
               JOIN folder_disposition_claims AS claim
                 ON claim.approval_id = approval.approval_id
               LEFT JOIN folder_disposition_settlements AS settlement
                 ON settlement.approval_id = claim.approval_id
               WHERE approval.run_id = r.run_id
                 AND settlement.approval_id IS NULL
           ) AS folder_unsettled,
           EXISTS (
               SELECT 1
               FROM subtitle_acquisition_requests AS request
               WHERE request.run_id = r.run_id
                 AND request.status IN ('approved', 'blocked')
           ) AS subtitle_unsettled
    FROM runs AS r
    JOIN discoveries AS d USING (discovery_id)
)
INSERT INTO legacy_effect_supersessions_v2
    (run_id, discovery_id, watch_id, source_folder,
     media_unsettled, folder_unsettled, subtitle_unsettled,
     fresh_scan_dispatched)
SELECT run_id, discovery_id, watch_id, source_folder,
       media_unsettled, folder_unsettled, subtitle_unsettled,
       source_folder IS NOT NULL
FROM affected
WHERE media_unsettled OR folder_unsettled OR subtitle_unsettled;

UPDATE runs AS run
SET status = 'superseded'
FROM legacy_effect_supersessions_v2 AS legacy
WHERE legacy.run_id = run.run_id
  AND run.status NOT IN ('completed', 'rolled_back');

UPDATE jobs AS job
SET status = 'completed', boot_id = NULL, updated_at = clock_timestamp()
FROM legacy_effect_supersessions_v2 AS legacy
WHERE legacy.run_id = job.run_id
  AND job.status IN ('pending', 'running');

UPDATE watch_folder_observations AS observation
SET discovery_id = NULL,
    status = 'settling',
    first_observed_at = clock_timestamp(),
    stable_at = NULL,
    blocked_reason = NULL,
    retry_count = 0
FROM legacy_effect_supersessions_v2 AS legacy
WHERE legacy.discovery_id = observation.discovery_id;

DELETE FROM run_operations AS operation
USING legacy_effect_supersessions_v2 AS legacy
WHERE legacy.run_id = operation.run_id
  AND operation.operation_kind IN (
      'manual_apply', 'automatic_apply', 'recover',
      'subtitle_acquire', 'subtitle_recover'
  );

INSERT INTO scheduler_audit (event_type, subject_id)
SELECT 'legacy_v1_superseded', run_id
FROM legacy_effect_supersessions_v2
ON CONFLICT (event_type, subject_id) DO NOTHING;

CREATE TRIGGER legacy_effect_supersessions_v2_immutable
    BEFORE UPDATE OR DELETE ON legacy_effect_supersessions_v2
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

REVOKE UPDATE, DELETE ON legacy_effect_supersessions_v2 FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT SELECT, INSERT
            ON legacy_effect_supersessions_v2 TO reeloom_app;
    END IF;
END;
$$;
