from __future__ import annotations


RUN_DELETION_READY_SQL = """
    r.status IN ('completed', 'failed', 'rolled_back')
    AND NOT EXISTS (
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
        FROM approval_claims AS claim
        LEFT JOIN approval_settlements AS settled
          ON settled.approval_id = claim.approval_id
        WHERE claim.run_id = r.run_id
          AND settled.approval_id IS NULL
    )
    AND NOT EXISTS (
        SELECT 1
        FROM folder_disposition_approvals AS approval
        JOIN folder_disposition_claims AS claim
          ON claim.approval_id = approval.approval_id
        LEFT JOIN folder_disposition_settlements AS settled
          ON settled.approval_id = claim.approval_id
        WHERE approval.run_id = r.run_id
          AND settled.approval_id IS NULL
    )
    AND (
        d.folder_generation_id IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM watch_folder_observations AS observed
            WHERE observed.discovery_id = d.discovery_id
              AND NOT (
                  observed.status = 'settled'
                  OR (
                      observed.status = 'blocked'
                      AND observed.blocked_reason =
                          'source_folder_missing'
                  )
              )
        )
    )
"""
