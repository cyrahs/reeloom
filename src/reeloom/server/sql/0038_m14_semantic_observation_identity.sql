ALTER TABLE watch_folder_observations
    DROP CONSTRAINT watch_folder_observations_check,
    ADD CONSTRAINT watch_folder_observations_check CHECK (
        (
            status = 'blocked'
            AND blocked_reason IS NOT NULL
        )
        OR (
            status <> 'blocked'
            AND blocked_reason IS NULL
            AND inventory_id IS NOT NULL
            AND inventory_payload IS NOT NULL
            AND snapshot_id IS NOT NULL
            AND snapshot_payload IS NOT NULL
            AND (
                (
                    folder_device IS NOT NULL
                    AND folder_inode IS NOT NULL
                )
                OR (
                    folder_device IS NULL
                    AND folder_inode IS NULL
                    AND inventory_id LIKE 'folder-inventory-v2:%'
                    AND snapshot_id LIKE 'candidate-snapshot-v2:%'
                )
            )
        )
    );
