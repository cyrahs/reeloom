-- Additive M14.6 repair.  0041 classified some candidate-snapshot-v1 runs
-- as forward-v2 because 0039 had backfilled an effect binding for every
-- historical media plan.  0043 corrected the mode and removed retired
-- coordination rows, but an interaction that was active during 0041 escaped
-- its legacy quarantine and would block both lifecycle actions and deletion.
-- Preserve the interaction as audit history while making the legacy run
-- terminal and removable.  This migration performs no filesystem effect.

UPDATE interactions AS interaction
SET status = 'failed',
    result = jsonb_build_object('error_code', 'legacy_effect_superseded'),
    finished_at = clock_timestamp()
FROM run_lifecycle_controls_v2 AS control
WHERE control.run_id = interaction.run_id
  AND control.mode = 'legacy_read_only'
  AND interaction.status = 'active';
