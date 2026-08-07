ALTER TABLE subtitle_successor_outbox
    ADD COLUMN stabilizing_inventory_id text,
    ADD COLUMN stabilizing_snapshot_id text;

ALTER TABLE subtitle_successor_outbox
    ADD CONSTRAINT subtitle_successor_stability_pair CHECK (
        (stabilizing_inventory_id IS NULL)
        = (stabilizing_snapshot_id IS NULL)
        AND (
            stabilizing_inventory_id IS NULL
            OR (
                octet_length(stabilizing_inventory_id) BETWEEN 1 AND 256
                AND octet_length(stabilizing_snapshot_id) BETWEEN 1 AND 256
            )
        )
    );
