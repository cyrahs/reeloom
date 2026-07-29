ALTER TABLE folder_disposition_transactions
    DROP CONSTRAINT folder_disposition_transactions_failure_code_check,
    ADD CONSTRAINT folder_disposition_transactions_failure_code_check CHECK (
        failure_code IN (
            'invalid_plan',
            'plan_not_found',
            'plan_already_exists',
            'plan_store_failure',
            'root_drift',
            'source_drift',
            'destination_collision',
            'symlink_not_allowed',
            'cross_filesystem',
            'preflight_failed',
            'transaction_busy',
            'journal_not_found',
            'invalid_journal',
            'journal_failure',
            'atomic_move_unsupported',
            'permission_denied',
            'transient_io',
            'state_ambiguous',
            'move_failed',
            'recovery_required'
        )
    );
