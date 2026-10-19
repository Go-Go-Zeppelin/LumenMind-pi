-- Persist fencing generations and the original terminal receipt.
ALTER TABLE memory_session ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_turn ADD COLUMN terminal_context_revision INTEGER;
ALTER TABLE memory_turn ADD COLUMN deleted_at TEXT;
CREATE INDEX memory_turn_pending ON memory_turn(namespace,status,conv_id);
CREATE INDEX memory_job_due ON memory_job(namespace,status,next_run_at);
