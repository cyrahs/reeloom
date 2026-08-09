ALTER TABLE subtitle_acquisition_requests
    DROP CONSTRAINT subtitle_acquisition_requests_check2,
    ADD CONSTRAINT subtitle_request_published_transaction CHECK (
        status <> 'published' OR transaction_id IS NOT NULL
    ),
    ADD CONSTRAINT subtitle_request_pre_effect_transaction CHECK (
        status NOT IN ('planned', 'approved') OR transaction_id IS NULL
    );

-- M14.5 keeps this table as a read-model projection.  A blocked request may
-- therefore carry the unified execution operation id that reached a terminal
-- unavailable state; it is no longer a legacy filesystem transaction/recovery
-- handle.
