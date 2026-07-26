ALTER TABLE interactions
    ADD COLUMN request_message text,
    ADD CONSTRAINT interactions_request_message_bounds
        CHECK (
            request_message IS NULL
            OR (
                octet_length(request_message) BETWEEN 1 AND 16384
            )
        );

CREATE FUNCTION reject_interaction_request_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.request_message IS DISTINCT FROM OLD.request_message THEN
        RAISE EXCEPTION 'interaction request cannot be mutated'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER interactions_request_immutable
    BEFORE UPDATE ON interactions
    FOR EACH ROW EXECUTE FUNCTION reject_interaction_request_mutation();
