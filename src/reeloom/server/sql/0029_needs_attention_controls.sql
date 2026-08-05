ALTER TABLE interactions
    ALTER COLUMN expected_plan_hash DROP NOT NULL,
    ADD COLUMN expected_event_sequence bigint
        CHECK (expected_event_sequence >= 1),
    ADD CONSTRAINT interactions_expected_head CHECK (
        (expected_plan_hash IS NOT NULL)
        <> (expected_event_sequence IS NOT NULL)
    );
