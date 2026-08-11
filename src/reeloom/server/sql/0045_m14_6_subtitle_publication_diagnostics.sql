ALTER TABLE subtitle_acquisition_requests
    DROP CONSTRAINT subtitle_failure_diagnostic_status;

ALTER TABLE subtitle_acquisition_requests
    ADD CONSTRAINT subtitle_failure_diagnostic_status CHECK (
        failure_diagnostic IS NULL
        OR (
            status = 'blocked'
            AND jsonb_typeof(failure_diagnostic) = 'object'
            AND octet_length(failure_diagnostic::text) <= 2048
            AND (
                (
                    failure_diagnostic ?& ARRAY[
                        'schema_version', 'stage', 'reason'
                    ]
                    AND (
                        failure_diagnostic - ARRAY[
                            'schema_version', 'stage', 'reason',
                            'actual_mode', 'actual_uid', 'entry_count',
                            'expected_policy', 'expected_uid', 'member_index'
                        ]::text[]
                    ) = '{}'::jsonb
                    AND failure_diagnostic->>'schema_version' = '1'
                    AND failure_diagnostic->>'stage' IN (
                        'destination_preflight', 'staging_prepare',
                        'staging_validate', 'member_write', 'publish'
                    )
                    AND failure_diagnostic->>'reason' IN (
                        'name_exists', 'create_failed',
                        'entry_type_mismatch', 'unsafe_permissions',
                        'owner_mismatch', 'not_empty',
                        'unexpected_entries', 'casefold_collision'
                    )
                )
                OR (
                    failure_diagnostic ?& ARRAY[
                        'schema_version', 'stage', 'reason'
                    ]
                    AND (
                        failure_diagnostic - ARRAY[
                            'schema_version', 'stage', 'reason'
                        ]::text[]
                    ) = '{}'::jsonb
                    AND failure_diagnostic->>'schema_version' = '2'
                    AND failure_diagnostic->>'stage' = 'publication'
                    AND failure_diagnostic->>'reason' IN (
                        'invalid_binding', 'no_follow_unavailable',
                        'directory_unreadable', 'unexpected_entry',
                        'casefold_collision', 'invalid_complete_marker',
                        'member_mismatch', 'content_unavailable',
                        'content_mismatch', 'member_post_write_mismatch',
                        'marker_post_write_mismatch',
                        'exclusive_name_exists', 'unsafe_path',
                        'filesystem_unavailable', 'collision', 'unsafe',
                        'unavailable'
                    )
                )
            )
        )
    );
