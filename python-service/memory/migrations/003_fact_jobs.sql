-- Retire preview per-turn jobs; the new scheduler selects fixed scope batches.
UPDATE memory_job SET status='CANCELLED',claim_token=NULL,lease_until=NULL,error_code='BATCH_PROTOCOL_UPGRADE' WHERE kind='fact_extract' AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED');
CREATE UNIQUE INDEX memory_fact_single_batch ON memory_job(namespace,user_id) WHERE kind='fact_extract' AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED');
ALTER TABLE memory_job ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
