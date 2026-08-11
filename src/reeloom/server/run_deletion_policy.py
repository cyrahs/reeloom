from __future__ import annotations


# This expression deliberately depends on the canonical v2 terminal facts,
# not on the historical Agent phase or whether optional housekeeping/rescan
# has completed.  It is embedded only in queries that alias runs/discoveries
# as ``r``/``d``.
RUN_DELETION_READY_SQL = """
    NOT EXISTS (
        SELECT 1 FROM run_operations AS operation
        WHERE operation.run_id = r.run_id
    )
    AND NOT EXISTS (
        SELECT 1 FROM interactions AS interaction
        WHERE interaction.run_id = r.run_id
          AND interaction.status = 'active'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM run_lifecycle_controls_v2 AS control
        JOIN execution_operations_v2 AS operation
          ON operation.operation_id = control.operation_id
        WHERE control.run_id = r.run_id
          AND operation.status IN ('authorized', 'running')
    )
    AND (
        EXISTS (
            SELECT 1
            FROM run_lifecycle_controls_v2 AS control
            LEFT JOIN execution_operations_v2 AS operation
              ON operation.operation_id = control.operation_id
            LEFT JOIN planning_terminal_results_v2 AS planning
              ON planning.run_id = control.run_id
            WHERE control.run_id = r.run_id
              AND control.mode = 'forward_v2'
              AND (
                  planning.run_id IS NOT NULL
                  OR operation.status IN (
                      'completed', 'partial', 'stale', 'collision',
                      'unsafe', 'unavailable', 'superseded'
                  )
              )
        )
        OR EXISTS (
            SELECT 1 FROM run_lifecycle_controls_v2 AS control
            WHERE control.run_id = r.run_id
              AND control.mode = 'legacy_read_only'
        )
        OR (
            NOT EXISTS (
                SELECT 1 FROM run_lifecycle_controls_v2 AS control
                WHERE control.run_id = r.run_id
            )
            AND r.status IN (
                'completed', 'failed', 'rolled_back', 'superseded'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM approval_claims AS claim
                LEFT JOIN approval_settlements AS settled
                  ON settled.approval_id = claim.approval_id
                WHERE claim.run_id = r.run_id
                  AND settled.approval_id IS NULL
            )
        )
    )
"""
