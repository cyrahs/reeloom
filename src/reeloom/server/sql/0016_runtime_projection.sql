ALTER TABLE run_states
    ADD COLUMN projection_schema text,
    ADD COLUMN projection_payload jsonb,
    ADD CONSTRAINT run_states_projection_pair
        CHECK (
            (projection_schema IS NULL) =
            (projection_payload IS NULL)
        );
