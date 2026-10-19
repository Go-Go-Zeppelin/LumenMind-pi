-- Initial authority schema. Safe for databases initialized by the preview release.
                CREATE TABLE IF NOT EXISTS memory_scope(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL,
                    next_source_seq INTEGER NOT NULL DEFAULT 1,
                    fact_revision INTEGER NOT NULL DEFAULT 0,
                    extract_cursor INTEGER NOT NULL DEFAULT 0,
                    erase_epoch INTEGER NOT NULL DEFAULT 0,
                    context_floor_source_seq INTEGER NOT NULL DEFAULT 0,
                    long_term_enabled INTEGER NOT NULL DEFAULT 1,
                    migration_state TEXT NOT NULL DEFAULT 'READY',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,user_id)
                );
                CREATE TABLE IF NOT EXISTS memory_session(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, conv_id TEXT NOT NULL,
                    next_turn_seq INTEGER NOT NULL DEFAULT 1,
                    context_revision INTEGER NOT NULL DEFAULT 0,
                    summary_generation INTEGER NOT NULL DEFAULT 0,
                    current_summary TEXT NOT NULL DEFAULT '',
                    lease_owner TEXT, lease_token TEXT, lease_until TEXT,
                    deleted_at TEXT,
                    PRIMARY KEY(namespace,user_id,conv_id),
                    FOREIGN KEY(namespace,user_id) REFERENCES memory_scope(namespace,user_id)
                );
                CREATE TABLE IF NOT EXISTS memory_turn(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    idempotency_key TEXT, conv_id TEXT NOT NULL, turn_seq INTEGER NOT NULL,
                    source_seq INTEGER NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT,
                    status TEXT NOT NULL, memory_eligible INTEGER NOT NULL DEFAULT 1,
                    payload_hash TEXT NOT NULL, lease_token TEXT, received_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY(namespace,user_id,request_id),
                    UNIQUE(namespace,user_id,conv_id,turn_seq),
                    UNIQUE(namespace,user_id,source_seq)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS memory_turn_idempotency
                    ON memory_turn(namespace,user_id,idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS memory_summary(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, conv_id TEXT NOT NULL,
                    generation INTEGER NOT NULL, covered_through_turn INTEGER NOT NULL,
                    base_context_revision INTEGER NOT NULL, erase_epoch INTEGER NOT NULL,
                    content TEXT NOT NULL, before_chars INTEGER NOT NULL DEFAULT 0,
                    after_chars INTEGER NOT NULL DEFAULT 0, prompt_version TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,user_id,conv_id,generation)
                );
                CREATE TABLE IF NOT EXISTS memory_fact(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, fact_id TEXT NOT NULL,
                    category TEXT NOT NULL, fact_key TEXT, content TEXT NOT NULL,
                    normalized_hash TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'BACKGROUND',
                    status TEXT NOT NULL DEFAULT 'CANDIDATE', provenance_state TEXT NOT NULL DEFAULT 'AVAILABLE',
                    valid_from TEXT NOT NULL, invalid_at TEXT, superseded_by TEXT,
                    revision INTEGER NOT NULL, erase_epoch INTEGER NOT NULL,
                    PRIMARY KEY(namespace,user_id,fact_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS memory_fact_active_key
                    ON memory_fact(namespace,user_id,fact_key) WHERE status='ACTIVE' AND fact_key IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS memory_fact_active_hash
                    ON memory_fact(namespace,user_id,normalized_hash) WHERE status='ACTIVE';
                CREATE TABLE IF NOT EXISTS memory_fact_source(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, fact_id TEXT NOT NULL,
                    source_seq INTEGER NOT NULL, evidence_excerpt TEXT NOT NULL,
                    PRIMARY KEY(namespace,user_id,fact_id,source_seq),
                    FOREIGN KEY(namespace,user_id,fact_id) REFERENCES memory_fact(namespace,user_id,fact_id)
                );
                CREATE TABLE IF NOT EXISTS memory_episode(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, episode_id TEXT NOT NULL,
                    conv_id TEXT NOT NULL, from_turn INTEGER NOT NULL, to_turn INTEGER NOT NULL,
                    summary TEXT NOT NULL, source_generation INTEGER NOT NULL,
                    invalid_at TEXT, erase_epoch INTEGER NOT NULL, index_version INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(namespace,user_id,episode_id)
                );
                CREATE TABLE IF NOT EXISTS memory_job(
                    namespace TEXT NOT NULL, user_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, dedup_key TEXT NOT NULL, from_seq INTEGER,
                    to_seq INTEGER, expected_revision INTEGER, expected_cursor INTEGER,
                    expected_epoch INTEGER, claim_token TEXT, lease_until TEXT,
                    status TEXT NOT NULL DEFAULT 'READY', attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_run_at TEXT NOT NULL, result TEXT, error_code TEXT, created_at TEXT NOT NULL,
                    UNIQUE(namespace,user_id,dedup_key), PRIMARY KEY(namespace,user_id,job_id)
                );
