ALTER TABLE runs
    ADD COLUMN agent_definition_hash text
        REFERENCES agent_definitions(definition_hash),
    ADD COLUMN session_id text;

CREATE UNIQUE INDEX runs_session_id_idx
    ON runs (session_id)
    WHERE session_id IS NOT NULL;
