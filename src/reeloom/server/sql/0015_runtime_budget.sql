ALTER TABLE run_states
    ADD COLUMN max_model_turns integer NOT NULL DEFAULT 64
        CHECK (max_model_turns > 0),
    ADD COLUMN max_tool_calls integer NOT NULL DEFAULT 64
        CHECK (max_tool_calls > 0),
    ADD COLUMN max_failures integer NOT NULL DEFAULT 3
        CHECK (max_failures > 0),
    ADD COLUMN max_total_tokens bigint NOT NULL DEFAULT 100000
        CHECK (max_total_tokens > 0);

ALTER TABLE run_states
    ADD CONSTRAINT run_states_model_turn_budget
        CHECK (model_turns <= max_model_turns),
    ADD CONSTRAINT run_states_tool_call_budget
        CHECK (tool_calls <= max_tool_calls),
    ADD CONSTRAINT run_states_failure_budget
        CHECK (failures <= max_failures),
    ADD CONSTRAINT run_states_token_budget
        CHECK (model_tokens <= max_total_tokens);
