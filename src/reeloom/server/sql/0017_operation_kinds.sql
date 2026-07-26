ALTER TABLE run_operations
    ADD CONSTRAINT run_operations_kind
        CHECK (
            operation_kind IN (
                'question',
                'revision',
                'reapply',
                'manual_apply',
                'automatic_apply',
                'recover'
            )
        );
