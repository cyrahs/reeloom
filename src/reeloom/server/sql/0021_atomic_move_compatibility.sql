ALTER TABLE folder_disposition_transactions
    ADD COLUMN failure_code text CHECK (
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
            'transient_io',
            'state_ambiguous',
            'move_failed',
            'recovery_required'
        )
    ),
    ADD COLUMN move_backend text NOT NULL DEFAULT 'native' CHECK (
        move_backend IN ('native', 'clouddrive_webdav')
    );
