"""Offline legacy import and fixed-job maintenance; dry-run is the CLI default.

No environment file, live Redis client, Chroma collection or model is accessed.
The snapshot format and operator procedure are documented in docs/MEMORY_MIGRATION.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .fact_pipeline import contains_sensitive
from .repository import ConflictError, MemoryRepository


class MigrationError(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError("Every exported message requires an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise MigrationError("Invalid exported ISO timestamp") from error
    # Legacy datetime.now() rows were naive. Treat them consistently as UTC for
    # ordering only; operators should normalize mixed-zone exports beforehand.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise MigrationError(f"{name} must contain 1 to 256 characters")
    value.encode("utf-8")
    return value


def _profile(value: Any) -> dict:
    # Reuse only the legacy parser/normalizer, never initialize its adapters.
    from .conversation_memory import MemoryManager
    if isinstance(value, str):
        value = MemoryManager._parse_profile_json(value)
    return MemoryManager._normalize_profile(value)


def plan_snapshot(payload: Any, namespace: str) -> dict:
    namespace = _identifier(namespace, "namespace")
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "scopes"} or payload["schema_version"] != 1 or not isinstance(payload["scopes"], list):
        raise MigrationError("Snapshot requires schema_version=1 and scopes array only")
    planned = []; seen_users = set()
    for scope in payload["scopes"]:
        if not isinstance(scope, dict) or set(scope) - {"user_id", "conversations", "profile", "profiles"}:
            raise MigrationError("Unsupported scope fields")
        uid = _identifier(scope.get("user_id"), "user_id")
        if uid in seen_users:
            raise MigrationError("A user scope may appear only once")
        seen_users.add(uid)
        conversations = scope.get("conversations", [])
        if not isinstance(conversations, list):
            raise MigrationError("conversations must be an array")
        source_fingerprint = _digest({"namespace": namespace, "scope": scope})
        turns = []; conv_plans = []; seen_convs = set(); skipped = 0
        for conv_position, conversation in enumerate(conversations):
            if not isinstance(conversation, dict) or set(conversation) - {"conv_id", "messages", "summary"}:
                raise MigrationError("Unsupported conversation fields")
            conv = _identifier(conversation.get("conv_id"), "conv_id")
            if conv in seen_convs:
                raise MigrationError("Duplicate conversation in scope")
            seen_convs.add(conv)
            messages = conversation.get("messages", [])
            if not isinstance(messages, list):
                raise MigrationError("messages must be an array")
            ordered = []
            for position, message in enumerate(messages):
                if not isinstance(message, dict) or set(message) - {"role", "content", "timestamp", "ts", "metadata"}:
                    raise MigrationError("Unsupported message fields")
                content = message.get("content")
                if not isinstance(content, str):
                    raise MigrationError("Message content must be a string")
                content.encode("utf-8")
                stamp = _timestamp(message.get("timestamp", message.get("ts")))
                ordered.append({"role": message.get("role"), "content": content, "timestamp": stamp, "position": position})
            ordered.sort(key=lambda item: (item["timestamp"], item["position"]))
            turn_seq = 0; index = 0
            while index < len(ordered):
                message = ordered[index]
                if message["role"] != "user":
                    skipped += 1; index += 1; continue
                turn_seq += 1
                assistant = ordered[index + 1] if index + 1 < len(ordered) and ordered[index + 1]["role"] == "assistant" else None
                request_id = "legacy-" + _digest([namespace, uid, conv, message["position"], message["timestamp"], message["content"]])[:40]
                turns.append({"request_id": request_id, "conv_id": conv, "turn_seq": turn_seq,
                    "user_text": message["content"], "assistant_text": assistant["content"] if assistant else None,
                    "status": "COMPLETED" if assistant else "INTERRUPTED", "received_at": message["timestamp"],
                    "finished_at": assistant["timestamp"] if assistant else message["timestamp"],
                    "order": (message["timestamp"], conv_position, message["position"])})
                index += 2 if assistant else 1
            legacy_summary = conversation.get("summary")
            summary = _json(legacy_summary) if isinstance(legacy_summary, (dict, list)) else legacy_summary
            if summary is not None and not isinstance(summary, str):
                raise MigrationError("Legacy summary must be text, object or array")
            conv_plans.append({"conv_id": conv, "next_turn_seq": turn_seq + 1, "legacy_summary": summary or ""})
        turns.sort(key=lambda item: item["order"])
        for seq, item in enumerate(turns, start=1):
            item["source_seq"] = seq
            item.pop("order")
        if "profile" in scope:
            profile = _profile(scope["profile"])
            profile_source = "USER_LEVEL"
        else:
            profiles = scope.get("profiles", [])
            if not isinstance(profiles, list):
                raise MigrationError("profiles must be an array")
            candidates = []
            for position, item in enumerate(profiles):
                if not isinstance(item, dict) or set(item) - {"conv_id", "timestamp", "ts", "profile"}:
                    raise MigrationError("Unsupported legacy profile fields")
                candidates.append((_timestamp(item.get("timestamp", item.get("ts"))), position, item.get("profile")))
            profile = _profile(max(candidates, default=("", 0, {}))[-1])
            profile_source = "LATEST_CONVERSATION" if candidates else "NONE"
        candidate_facts = [{"category": "preference", "content": item} for item in profile["preferences"]]
        candidate_facts.extend({"category": "stable_context", "content": f"{key}: {value}"[:300]}
            for key, values in profile["entities"].items() for value in values)
        # Candidate IDs are reproducible and never occupy the ACTIVE uniqueness
        # indexes or activate unsupported legacy facts without user evidence.
        unique = {}; skipped_sensitive = 0
        for candidate in candidate_facts:
            if contains_sensitive(candidate["content"]):
                skipped_sensitive += 1
                continue
            digest = _digest([candidate["category"], candidate["content"]])
            unique[digest] = {**candidate, "fact_id": "legacy-fact-" + digest[:40]}
        planned.append({"namespace": namespace, "user_id": uid, "fingerprint": source_fingerprint,
            "turns": turns, "conversations": conv_plans, "candidate_facts": list(unique.values()),
            "skipped_non_user_messages": skipped, "skipped_sensitive_candidates": skipped_sensitive, "profile_source": profile_source})
    return {"namespace": namespace, "scopes": planned}


def plan_report(plan: dict) -> dict:
    return {"namespace": plan["namespace"], "scopes": [
        {"user_id": scope["user_id"], "fingerprint": scope["fingerprint"], "turns": len(scope["turns"]),
         "completed": sum(item["status"] == "COMPLETED" for item in scope["turns"]),
         "interrupted": sum(item["status"] == "INTERRUPTED" for item in scope["turns"]),
         "candidate_facts": len(scope["candidate_facts"]),
         "legacy_summaries": sum(bool(item["legacy_summary"]) for item in scope["conversations"]),
         "skipped_non_user_messages": scope["skipped_non_user_messages"], "skipped_sensitive_candidates": scope["skipped_sensitive_candidates"], "profile_source": scope["profile_source"]}
        for scope in plan["scopes"]]}


def backup_database(database_path: str | Path, backup_dir: str | Path) -> Path:
    source_path = Path(database_path).expanduser().resolve()
    if source_path.is_file():
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as check:
            names = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names.intersection({"orders", "bills"}):
                raise MigrationError("Destination contains business tables; memory migration is refused")
    destination_dir = Path(backup_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    name = source_path.name + "." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "." + secrets.token_hex(4) + ".backup.sqlite3"
    backup_path = destination_dir / name
    backup_path.touch(mode=0o600, exist_ok=False)
    # SQLite online backup also copies committed WAL pages. A fresh destination
    # is backed up as an empty database before authority migrations initialize it.
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    return backup_path


class LegacyImporter:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def _prepare(self, scope: dict) -> Optional[dict]:
        repo = self.repository; uid = scope["user_id"]; fingerprint = scope["fingerprint"]
        with repo._lock, repo._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            repo._ensure_scope(db, uid)
            state = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (repo.namespace, uid)).fetchone()
            deleted = db.execute("SELECT 1 FROM memory_session WHERE namespace=? AND user_id=? AND deleted_at IS NOT NULL LIMIT 1", (repo.namespace, uid)).fetchone()
            if state["migration_state"] == "ERASED" or state["context_floor_source_seq"] > 0 or state["erase_epoch"] > 0 or deleted:
                raise ConflictError("Deletion/forgetting barrier forbids legacy import")
            audit = db.execute("SELECT * FROM memory_job WHERE namespace=? AND user_id=? AND dedup_key=?", (repo.namespace, uid, f"legacy-import:{fingerprint}")).fetchone()
            if audit and audit["status"] == "WRITTEN" and state["migration_state"] == "LEGACY_IMPORTED":
                result = json.loads(audit["result"]); db.commit(); return {**result, "replayed": True}
            existing = db.execute("SELECT 1 FROM memory_turn WHERE namespace=? AND user_id=? LIMIT 1", (repo.namespace, uid)).fetchone()
            if state["next_source_seq"] != 1 or existing or state["migration_state"] == "LEGACY_IMPORTED":
                raise ConflictError("Scope already contains traffic or a different legacy import")
            if state["migration_state"] == "IMPORTING" and not audit:
                raise ConflictError("Scope is locked by a different import fingerprint")
            # Commit the maintenance fence separately. Any later data failure
            # rolls back the body transaction while leaving this lock in place.
            db.execute("UPDATE memory_scope SET migration_state='IMPORTING' WHERE namespace=? AND user_id=?", (repo.namespace, uid))
            now = repo._db_now(db)
            db.execute("INSERT OR IGNORE INTO memory_job(namespace,user_id,job_id,kind,dedup_key,status,next_run_at,result,created_at) VALUES(?,?,?,?,?,'PROCESSING',?,?,?)", (repo.namespace, uid, secrets.token_hex(16), "legacy_import", f"legacy-import:{fingerprint}", now, _json({"status": "IMPORTING", "fingerprint": fingerprint}), now))
            db.commit()
        return None

    def _write_scope(self, db, scope: dict) -> dict:
        repo = self.repository; uid = scope["user_id"]; now = repo._db_now(db)
        for conversation in scope["conversations"]:
            conv = conversation["conv_id"]
            repo._ensure_session(db, uid, conv)
            db.execute("UPDATE memory_session SET next_turn_seq=?,context_revision=?,summary_generation=0,current_summary='' WHERE namespace=? AND user_id=? AND conv_id=?", (conversation["next_turn_seq"], conversation["next_turn_seq"] - 1, repo.namespace, uid, conv))
            if conversation["legacy_summary"]:
                db.execute("INSERT INTO memory_summary(namespace,user_id,conv_id,generation,covered_through_turn,base_context_revision,erase_epoch,content,before_chars,after_chars,prompt_version,created_at) VALUES(?,?,?,1,0,0,0,?,0,?,'LEGACY_IMPORT',?)", (repo.namespace, uid, conv, conversation["legacy_summary"], len(conversation["legacy_summary"]), now))
        for item in scope["turns"]:
            db.execute("INSERT INTO memory_turn(namespace,user_id,request_id,conv_id,turn_seq,source_seq,user_text,assistant_text,status,memory_eligible,payload_hash,received_at,finished_at,terminal_context_revision) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?)", (repo.namespace, uid, item["request_id"], item["conv_id"], item["turn_seq"], item["source_seq"], item["user_text"], item["assistant_text"], item["status"], repo._payload_hash(item["conv_id"], item["user_text"]), item["received_at"], item["finished_at"], item["turn_seq"]))
        for fact in scope["candidate_facts"]:
            normalized_hash = hashlib.sha256(" ".join(fact["content"].split()).casefold().encode("utf-8")).hexdigest()
            db.execute("INSERT INTO memory_fact(namespace,user_id,fact_id,category,content,normalized_hash,origin,status,provenance_state,valid_from,revision,erase_epoch) VALUES(?,?,?,?,?,?,'LEGACY_IMPORT','CANDIDATE','LEGACY_UNVERIFIED',?,0,0)", (repo.namespace, uid, fact["fact_id"], fact["category"], fact["content"], normalized_hash, now))
        result = {"status": "LEGACY_IMPORTED", "fingerprint": scope["fingerprint"],
            "turns": len(scope["turns"]), "candidate_facts": len(scope["candidate_facts"]),
            "skipped_non_user_messages": scope["skipped_non_user_messages"], "skipped_sensitive_candidates": scope["skipped_sensitive_candidates"], "replayed": False}
        db.execute("UPDATE memory_scope SET next_source_seq=?,extract_cursor=?,migration_state='LEGACY_IMPORTED',updated_at=? WHERE namespace=? AND user_id=?", (len(scope["turns"]) + 1, len(scope["turns"]), now, repo.namespace, uid))
        db.execute("UPDATE memory_job SET status='WRITTEN',result=? WHERE namespace=? AND user_id=? AND dedup_key=?", (_json(result), repo.namespace, uid, f"legacy-import:{scope['fingerprint']}"))
        return result

    def import_scope(self, scope: dict) -> dict:
        if scope["namespace"] != self.repository.namespace:
            raise MigrationError("Import plan namespace does not match server repository namespace")
        replay = self._prepare(scope)
        if replay is not None:
            return replay
        with self.repository._lock, self.repository._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.repository.namespace, scope["user_id"])).fetchone()
            audit = db.execute("SELECT * FROM memory_job WHERE namespace=? AND user_id=? AND dedup_key=?", (self.repository.namespace, scope["user_id"], f"legacy-import:{scope['fingerprint']}")).fetchone()
            # A simultaneous identical importer may have committed after prepare.
            if state and state["migration_state"] == "LEGACY_IMPORTED" and audit and audit["status"] == "WRITTEN":
                db.commit(); return {**json.loads(audit["result"]), "replayed": True}
            if not state or state["migration_state"] != "IMPORTING" or not audit or audit["status"] != "PROCESSING" or state["next_source_seq"] != 1 or state["erase_epoch"] != 0:
                raise ConflictError("Import maintenance fence changed before the body transaction")
            result = self._write_scope(db, scope)
            db.commit(); return result


def apply_snapshot(payload: Any, database_path: str | Path, namespace: str, backup_dir: str | Path) -> dict:
    plan = plan_snapshot(payload, namespace)
    backup_path = backup_database(database_path, backup_dir)
    repository = MemoryRepository(str(database_path), namespace)
    importer = LegacyImporter(repository)
    results = [importer.import_scope(scope) for scope in plan["scopes"]]
    return {"mode": "applied", "backup": str(backup_path), "scopes": results}


def requeue_job(repository: MemoryRepository, user_id: str, job_id: str) -> dict:
    with repository._lock, repository._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM memory_job WHERE namespace=? AND user_id=? AND job_id=?", (repository.namespace, user_id, job_id)).fetchone()
        if not job or job["status"] != "QUARANTINED" or job["kind"] not in {"fact_extract", "summary", "index_upsert", "index_delete"}:
            raise ConflictError("Only an existing quarantined fixed pipeline job can be requeued")
        now = repository._db_now(db)
        result = {"status": "REQUEUED", "job_id": job_id, "from_seq": job["from_seq"], "to_seq": job["to_seq"], "previous_failure_count": job["failure_count"]}
        db.execute("UPDATE memory_job SET status='RETRY_WAIT',failure_count=0,next_run_at=?,claim_token=NULL,lease_until=NULL,error_code='MANUAL_REQUEUE' WHERE namespace=? AND user_id=? AND job_id=?", (now, repository.namespace, user_id, job_id))
        audit_id = secrets.token_hex(16)
        db.execute("INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,status,next_run_at,result,created_at) VALUES(?,?,?,?,?,'WRITTEN',?,?,?)", (repository.namespace, user_id, audit_id, "maintenance_requeue", f"requeue:{job_id}:{audit_id}", now, _json(result), now))
        db.commit(); return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Explicit destination SQLite path; no .env is loaded")
    parser.add_argument("--namespace", required=True, help="Server-controlled namespace")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--snapshot", help="Offline JSON export")
    action.add_argument("--requeue-job", help="Quarantined job ID; preserves its fixed source range")
    parser.add_argument("--user-id", help="Required with --requeue-job")
    parser.add_argument("--apply", action="store_true", help="Write after taking a SQLite online backup")
    parser.add_argument("--backup-dir", help="Required for --apply")
    args = parser.parse_args(argv)
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    if args.requeue_job and not args.user_id:
        parser.error("--requeue-job requires --user-id")
    if args.snapshot:
        with Path(args.snapshot).open(encoding="utf-8") as source:
            payload = json.load(source)
        if args.apply:
            result = apply_snapshot(payload, args.database, args.namespace, args.backup_dir)
        else:
            result = {"mode": "dry-run", **plan_report(plan_snapshot(payload, args.namespace))}
    elif args.apply:
        backup_path = backup_database(args.database, args.backup_dir)
        repository = MemoryRepository(args.database, args.namespace)
        result = {"backup": str(backup_path), **requeue_job(repository, args.user_id, args.requeue_job)}
    else:
        # A plan describes the requested action without opening/migrating a DB.
        result = {"mode": "dry-run", "action": "requeue", "user_id": args.user_id, "job_id": args.requeue_job}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
