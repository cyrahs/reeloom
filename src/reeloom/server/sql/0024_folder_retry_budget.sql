ALTER TABLE watch_folder_observations
    ADD COLUMN retry_count integer NOT NULL DEFAULT 0
        CHECK (retry_count >= 0 AND retry_count <= 3);
