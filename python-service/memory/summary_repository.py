"""Durable complete-turn compaction with short optimistic commit transactions."""
from __future__ import annotations

import json
import secrets
from typing import Any, Optional

from .compactor import SUMMARY_FIELDS
from .context_builder import _encode
from .fact_pipeline import contains_sensitive

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
_PROMPT_VERSION = "summary-v1"


def validate_summary_result(value: Any, max_chars: int = 4000) -> dict:
    if not isinstance(value, dict) or set(value) != set(SUMMARY_FIELDS):
        raise ValueError("Summary must contain exactly the six supported fields")
    for item in value.values():
        if not isinstance(item, str) and not (isinstance(item, list) and all(isinstance(part, str) for part in item)):
            raise ValueError("Summary fields must be strings or flat string arrays")
    if not any(item.strip() if isinstance(item, str) else any(part.strip() for part in item) for item in value.values()):
        raise ValueError("Summary must not be empty")
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if contains_sensitive(rendered):
        raise ValueError("SENSITIVE_CONTENT")
    if len(_encode({"summary": value})) > max_chars:
        raise ValueError("Summary exceeds its character budget")
    rendered.encode("utf-8")
    return value


def _metadata(row) -> dict:
    return json.loads(row["dedup_key"])


def _turn_data(row) -> dict:
    return {"turn_seq": row["turn_seq"], "source_seq": row["source_seq"],
            "status": row["status"], "user_text": row["user_text"],
            "assistant_text": row["assistant_text"] if row["status"] == "COMPLETED" else None,
            "received_at": row["received_at"], "finished_at": row["finished_at"]}


class SummaryRepositoryMixin:
    def _summary_baseline(self, db, session, scope):
        # Session points to the valid audit generation; old epochs remain audit
        # rows but are never reused as model input after a forgetting barrier.
        row = db.execute("SELECT * FROM memory_summary WHERE namespace=? AND user_id=? AND conv_id=? AND generation=? AND erase_epoch=?",
            (self.namespace, session["user_id"], session["conv_id"], session["summary_generation"], scope["erase_epoch"])).fetchone()
        return row

    def schedule_due_summary_jobs(self, trigger_chars: int = 18_000, trigger_turns: int = 12,
                                  recent_min_turns: int = 3, max_batch_chars: int = 16_000) -> int:
        if min(trigger_chars, trigger_turns, recent_min_turns, max_batch_chars) <= 0:
            raise ValueError("Summary scheduling limits must be positive")
        scheduled = 0
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sessions = db.execute("SELECT * FROM memory_session WHERE namespace=? AND deleted_at IS NULL", (self.namespace,)).fetchall()
            for session in sessions:
                uid, conv = session["user_id"], session["conv_id"]
                active = db.execute("SELECT dedup_key FROM memory_job WHERE namespace=? AND user_id=? AND kind='summary' AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED')", (self.namespace, uid)).fetchall()
                if any(_metadata(job)["conv_id"] == conv for job in active):
                    continue
                scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace, uid)).fetchone()
                if scope["migration_state"] == "IMPORTING":
                    continue
                baseline = self._summary_baseline(db, session, scope)
                covered = baseline["covered_through_turn"] if baseline else 0
                rows = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=? AND turn_seq>? AND source_seq>? AND deleted_at IS NULL ORDER BY turn_seq",
                    (self.namespace, uid, conv, covered, scope["context_floor_source_seq"])).fetchall()
                terminal = []
                for row in rows:
                    if row["status"] not in _TERMINAL:
                        break  # An in-flight turn is an indivisible barrier.
                    terminal.append(row)
                before_chars = len(baseline["content"]) if baseline else 0
                before_chars += sum(len(row["user_text"]) + len(row["assistant_text"] or "") for row in terminal)
                if len(terminal) <= recent_min_turns or (before_chars <= trigger_chars and len(terminal) <= trigger_turns):
                    continue
                chosen = []; used = 0
                for row in terminal[:-recent_min_turns]:
                    size = len(json.dumps(_turn_data(row), ensure_ascii=False, separators=(",", ":")))
                    if chosen and (used + size > max_batch_chars or len(chosen) >= 50):
                        break
                    chosen.append(row); used += size
                if not chosen:
                    continue
                metadata = dict(kind="summary", conv_id=conv, generation=session["summary_generation"],
                    covered_through_turn=covered, context_revision=session["context_revision"], erase_epoch=scope["erase_epoch"])
                now = self._db_now(db)
                db.execute("INSERT OR IGNORE INTO memory_job(namespace,user_id,job_id,kind,dedup_key,from_seq,to_seq,expected_revision,expected_cursor,expected_epoch,next_run_at,result,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.namespace, uid, secrets.token_hex(16), "summary", json.dumps(metadata, separators=(",", ":")), chosen[0]["turn_seq"], chosen[-1]["turn_seq"], session["context_revision"], covered, scope["erase_epoch"], now,
                     json.dumps({"conv_id": conv, "base_generation": session["summary_generation"]}), now))
                scheduled += db.execute("SELECT changes()").fetchone()[0]
            db.commit()
        return scheduled

    def claim_summary_job(self, lease_seconds: int = 180) -> Optional[dict]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM memory_job WHERE namespace=? AND kind='summary' AND status IN ('READY','RETRY_WAIT','PROCESSING') AND julianday(next_run_at)<=julianday('now') AND (status!='PROCESSING' OR julianday(lease_until)<=julianday('now')) ORDER BY created_at", (self.namespace,)).fetchall()
            for row in rows:
                meta = _metadata(row)
                session = db.execute("SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace, row["user_id"], meta["conv_id"])).fetchone()
                scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace, row["user_id"])).fetchone()
                baseline = self._summary_baseline(db, session, scope) if session and scope else None
                covered = baseline["covered_through_turn"] if baseline else 0
                if (not session or session["deleted_at"] or not scope or scope["migration_state"] == "IMPORTING" or scope["erase_epoch"] != row["expected_epoch"] or
                    session["context_revision"] != row["expected_revision"] or session["summary_generation"] != meta["generation"] or covered != row["expected_cursor"]):
                    db.execute("UPDATE memory_job SET status='CANCELLED',error_code='STALE_BASELINE',claim_token=NULL,lease_until=NULL WHERE namespace=? AND job_id=?", (self.namespace, row["job_id"]))
                    continue
                turns = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=? AND turn_seq BETWEEN ? AND ? AND source_seq>? AND deleted_at IS NULL ORDER BY turn_seq", (self.namespace, row["user_id"], meta["conv_id"], row["from_seq"], row["to_seq"], scope["context_floor_source_seq"])).fetchall()
                if not turns or any(turn["status"] not in _TERMINAL for turn in turns) or len(turns) != row["to_seq"] - row["from_seq"] + 1:
                    db.execute("UPDATE memory_job SET status='CANCELLED',error_code='SOURCE_CHANGED' WHERE namespace=? AND job_id=?", (self.namespace, row["job_id"]))
                    continue
                token = secrets.token_urlsafe(24)
                db.execute("UPDATE memory_job SET status='PROCESSING',claim_token=?,lease_until=?,attempt_count=attempt_count+1 WHERE namespace=? AND job_id=?", (token, self._db_now(db, max(1, lease_seconds)), self.namespace, row["job_id"]))
                job = dict(row)
                job.update(claim_token=token, conv_id=meta["conv_id"], base_generation=meta["generation"],
                           previous_summary=json.loads(baseline["content"]) if baseline else {}, turns=[_turn_data(turn) for turn in turns])
                db.commit(); return job
            db.commit()
        return None

    def apply_summary_result(self, job_id: str, claim_token: str, payload: Any, max_chars: int = 4000) -> dict:
        from .repository import ConflictError
        value = validate_summary_result(payload, max_chars)
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM memory_job WHERE namespace=? AND job_id=? AND kind='summary'", (self.namespace, job_id)).fetchone()
            if job and job["status"] == "WRITTEN" and job["claim_token"] == claim_token:
                db.commit(); return json.loads(job["result"])
            if not job or job["status"] != "PROCESSING" or job["claim_token"] != claim_token or not db.execute("SELECT julianday(?)>julianday('now')", (job["lease_until"],)).fetchone()[0]:
                raise ConflictError("Summary job claim expired")
            meta = _metadata(job); uid = job["user_id"]; conv = meta["conv_id"]
            session = db.execute("SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace, uid, conv)).fetchone()
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace, uid)).fetchone()
            baseline = self._summary_baseline(db, session, scope) if session and scope else None
            covered = baseline["covered_through_turn"] if baseline else 0
            if (not session or session["deleted_at"] or not scope or scope["migration_state"] == "IMPORTING" or scope["erase_epoch"] != job["expected_epoch"] or
                session["context_revision"] != job["expected_revision"] or session["summary_generation"] != meta["generation"] or covered != job["expected_cursor"]):
                raise ConflictError("Summary session baseline changed")
            turns = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=? AND turn_seq BETWEEN ? AND ? AND source_seq>? AND deleted_at IS NULL ORDER BY turn_seq", (self.namespace, uid, conv, job["from_seq"], job["to_seq"], scope["context_floor_source_seq"])).fetchall()
            if not turns or len(turns) != job["to_seq"] - job["from_seq"] + 1 or any(turn["status"] not in _TERMINAL for turn in turns):
                raise ConflictError("Summary complete-turn sources changed")
            generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM memory_summary WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace, uid, conv)).fetchone()[0]
            before_chars = len(baseline["content"]) if baseline else 0
            before_chars += sum(len(turn["user_text"]) + len(turn["assistant_text"] or "") for turn in turns)
            now = self._db_now(db)
            db.execute("INSERT INTO memory_summary(namespace,user_id,conv_id,generation,covered_through_turn,base_context_revision,erase_epoch,content,before_chars,after_chars,prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (self.namespace, uid, conv, generation, job["to_seq"], session["context_revision"], scope["erase_epoch"], rendered, before_chars, len(rendered), _PROMPT_VERSION, now))
            db.execute("UPDATE memory_session SET summary_generation=?,current_summary=?,context_revision=context_revision+1 WHERE namespace=? AND user_id=? AND conv_id=?", (generation, rendered, self.namespace, uid, conv))
            # Rolling output includes the previous summary; its provenance covers
            # the whole visible summarized prefix, not just this delta batch.
            prefix = db.execute("SELECT MIN(turn_seq),MIN(memory_eligible) FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=? AND turn_seq<=? AND source_seq>? AND deleted_at IS NULL", (self.namespace, uid, conv, job["to_seq"], scope["context_floor_source_seq"])).fetchone()
            episode_id = None
            if self.long_term_enabled and scope["long_term_enabled"] and uid.strip().casefold() not in {"anonymous", "demo"} and prefix[1] == 1:
                episode_id = f"summary-{secrets.token_hex(16)}"
                db.execute("INSERT INTO memory_episode(namespace,user_id,episode_id,conv_id,from_turn,to_turn,summary,source_generation,erase_epoch,index_version) VALUES(?,?,?,?,?,?,?,?,?,1)", (self.namespace, uid, episode_id, conv, prefix[0], job["to_seq"], rendered, generation, scope["erase_epoch"]))
                db.execute("INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,expected_epoch,next_run_at,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (self.namespace, uid, secrets.token_hex(16), "index_upsert", f"index-upsert:{episode_id}:1", scope["erase_epoch"], now, json.dumps({"episode_id": episode_id, "index_version": 1}), now))
            result = {"status": "WRITTEN", "generation": generation, "covered_through_turn": job["to_seq"], "context_revision": session["context_revision"] + 1, "episode_id": episode_id}
            db.execute("UPDATE memory_job SET status='WRITTEN',result=?,lease_until=NULL WHERE namespace=? AND job_id=?", (json.dumps(result), self.namespace, job_id))
            db.commit(); return result

    def fail_summary_job(self, job_id: str, claim_token: str, error: str,
                         conflict: bool = False, max_attempts: int = 3) -> dict:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM memory_job WHERE namespace=? AND job_id=? AND kind='summary' AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')", (self.namespace, job_id, claim_token)).fetchone()
            if not job:
                db.commit(); return {"status": "STALE"}
            failures = job["failure_count"] + int(not conflict)
            status = "CANCELLED" if conflict else "QUARANTINED" if failures >= max_attempts else "RETRY_WAIT"
            db.execute("UPDATE memory_job SET status=?,failure_count=?,error_code=?,lease_until=NULL,next_run_at=? WHERE namespace=? AND job_id=?", (status, failures, error[:120], self._db_now(db, min(300, 2 ** max(1, failures))), self.namespace, job_id))
            db.commit(); return {"status": status, "failure_count": failures}
