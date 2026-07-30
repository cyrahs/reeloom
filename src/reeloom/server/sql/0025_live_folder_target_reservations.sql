DROP INDEX folder_disposition_target_reservation;

CREATE INDEX folder_disposition_target_history_lookup
    ON folder_disposition_plans (
        source_root_device, source_root_inode, action, target_name_key
    )
    WHERE target_name_key IS NOT NULL;
