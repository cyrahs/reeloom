DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
    ) THEN
        GRANT USAGE ON SCHEMA public TO reeloom_app;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA public TO reeloom_app;
        GRANT USAGE, SELECT
            ON ALL SEQUENCES IN SCHEMA public TO reeloom_app;
        GRANT EXECUTE
            ON ALL FUNCTIONS IN SCHEMA public TO reeloom_app;
    END IF;
END;
$$;
