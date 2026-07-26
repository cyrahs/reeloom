ALTER TABLE approvals
    DROP CONSTRAINT approvals_run_id_plan_hash_key,
    ADD CONSTRAINT approvals_id_binding_key
        UNIQUE (approval_id, run_id, plan_hash);

ALTER TABLE approval_claims
    ADD COLUMN run_id text,
    ADD COLUMN plan_hash text;

UPDATE approval_claims AS c
SET run_id = a.run_id, plan_hash = a.plan_hash
FROM approvals AS a
WHERE a.approval_id = c.approval_id;

ALTER TABLE approval_claims
    ALTER COLUMN run_id SET NOT NULL,
    ALTER COLUMN plan_hash SET NOT NULL,
    ADD CONSTRAINT approval_claims_binding_fkey
        FOREIGN KEY (approval_id, run_id, plan_hash)
        REFERENCES approvals(approval_id, run_id, plan_hash),
    ADD CONSTRAINT approval_claims_run_plan_key
        UNIQUE (run_id, plan_hash);
