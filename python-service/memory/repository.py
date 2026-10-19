"""SQLite 记忆权威存储。

Redis/Chroma 在本项目中都是可重建的加速层；该模块保存所有必须可恢复的
原文、版本、删除屏障和持久任务。每个公开操作只使用一个短事务连接，
避免在线程池之间共享事务连接。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .models import MemoryScope, MemorySnapshot, TurnReceipt
from .fact_repository import FactRepositoryMixin
from .summary_repository import SummaryRepositoryMixin
from .episodic_index import EpisodicIndexRepositoryMixin, validate_episode_candidates


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RepositoryError(RuntimeError):
    pass


class ConflictError(RepositoryError):
    """请求幂等键、版本或正文与已提交状态冲突。"""


class LeaseError(RepositoryError):
    """会话租约已经失效或不属于当前请求。"""


class CapacityError(RepositoryError):
    pass


class MemoryRepository(FactRepositoryMixin, SummaryRepositoryMixin, EpisodicIndexRepositoryMixin):
    SCHEMA_VERSION = 3

    def __init__(self, path: str = "data/memory/memory.sqlite3", namespace: str = "lumenmind", long_term_enabled: bool = True):
        self.path = str(Path(path).expanduser())
        self.namespace = namespace.strip() or "lumenmind"
        self.long_term_enabled = bool(long_term_enabled)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("CREATE TABLE IF NOT EXISTS memory_schema_version(version INTEGER NOT NULL)")
                row = db.execute("SELECT MAX(version) FROM memory_schema_version").fetchone()
                current = int(row[0] or 0)
                if current > self.SCHEMA_VERSION:
                    raise RepositoryError("数据库版本高于当前服务，请使用兼容版本")
                for migration in sorted(Path(__file__).with_name("migrations").glob("[0-9]*_*.sql")):
                    version = int(migration.name.split("_", 1)[0])
                    if version <= current:
                        continue
                    # execute() preserves the single transaction; executescript() commits early.
                    statement = ""
                    for line in migration.read_text(encoding="utf-8").splitlines(True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            db.execute(statement)
                            statement = ""
                    db.execute("DELETE FROM memory_schema_version")
                    db.execute("INSERT INTO memory_schema_version VALUES(?)", (version,))
                db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _db_now(db: sqlite3.Connection, seconds: int = 0) -> str:
        return str(db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now',?)", (f"{seconds:+d} seconds",)).fetchone()[0])

    def _ensure_scope(self, db: sqlite3.Connection, user_id: str) -> None:
        now = utc_now()
        db.execute(
            """INSERT INTO memory_scope(namespace,user_id,created_at,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(namespace,user_id) DO NOTHING""",
            (self.namespace, user_id, now, now),
        )

    def _ensure_session(self, db: sqlite3.Connection, user_id: str, conv_id: str) -> sqlite3.Row:
        self._ensure_scope(db, user_id)
        db.execute(
            """INSERT INTO memory_session(namespace,user_id,conv_id)
               VALUES(?,?,?) ON CONFLICT(namespace,user_id,conv_id) DO NOTHING""",
            (self.namespace, user_id, conv_id),
        )
        row = db.execute(
            "SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?",
            (self.namespace, user_id, conv_id),
        ).fetchone()
        assert row is not None
        return row

    @staticmethod
    def _payload_hash(conv_id: str, message: str) -> str:
        return hashlib.sha256(f"{conv_id}\0{message}".encode("utf-8")).hexdigest()

    def _receipt(self, db: sqlite3.Connection, row: sqlite3.Row, replayed: bool = False) -> TurnReceipt:
        session = db.execute("SELECT context_revision,lease_until,lease_owner FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,row["user_id"],row["conv_id"])).fetchone()
        revision = row["terminal_context_revision"]
        return TurnReceipt(row["request_id"], self.namespace, row["user_id"], row["conv_id"], row["turn_seq"], row["source_seq"], row["status"], row["lease_token"] or "", int(revision if revision is not None else session["context_revision"] if session else 0), response=row["assistant_text"], lease_until=session["lease_until"] if session and session["lease_owner"] == row["request_id"] else None, replayed=replayed)

    def _enqueue_turn(self, db: sqlite3.Connection, row: sqlite3.Row) -> None:
        # The scheduler scans durable turns and selects a contiguous scope batch.
        # Per-turn jobs would bypass minimum count/age and break cursor ordering.
        return None

    def _interrupt_expired(self, db: sqlite3.Connection, user_id: Optional[str] = None, conv_id: Optional[str] = None) -> int:
        query = """SELECT t.* FROM memory_turn t JOIN memory_session s ON s.namespace=t.namespace AND s.user_id=t.user_id AND s.conv_id=t.conv_id
                   WHERE t.namespace=? AND t.status='RECEIVED' AND (s.lease_until IS NULL OR julianday(s.lease_until)<=julianday('now') OR s.lease_owner!=t.request_id OR s.lease_token!=t.lease_token)"""
        params: List[Any] = [self.namespace]
        if user_id is not None:
            query += " AND t.user_id=?"; params.append(user_id)
        if conv_id is not None:
            query += " AND t.conv_id=?"; params.append(conv_id)
        rows = db.execute(query,params).fetchall()
        for row in rows:
            db.execute("UPDATE memory_session SET context_revision=context_revision+1,lease_owner=CASE WHEN lease_owner=? THEN NULL ELSE lease_owner END,lease_token=CASE WHEN lease_owner=? THEN NULL ELSE lease_token END,lease_until=CASE WHEN lease_owner=? THEN NULL ELSE lease_until END WHERE namespace=? AND user_id=? AND conv_id=?", (row["request_id"],row["request_id"],row["request_id"],self.namespace,row["user_id"],row["conv_id"]))
            db.execute("UPDATE memory_turn SET status='INTERRUPTED',finished_at=?,terminal_context_revision=(SELECT context_revision FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?) WHERE namespace=? AND user_id=? AND request_id=?", (self._db_now(db),self.namespace,row["user_id"],row["conv_id"],self.namespace,row["user_id"],row["request_id"]))
            self._enqueue_turn(db,row)
        return len(rows)

    def begin_turn(self, user_id: str, conv_id: Optional[str], request_id: str, message: str,
                   idempotency_key: Optional[str] = None, lease_seconds: int = 90) -> TurnReceipt:
        if not user_id.strip() or not request_id.strip() or (conv_id is not None and not conv_id.strip()):
            raise ValueError("user_id、request_id 及显式 conv_id 不能为空")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            idem = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND idempotency_key=?", (self.namespace,user_id,idempotency_key)).fetchone() if idempotency_key else None
            if existing and idem and existing["request_id"] != idem["request_id"]:
                raise ConflictError("request_id 和 Idempotency-Key 指向不同请求")
            existing = existing or idem
            if existing:
                mapped_conv = existing["conv_id"]
                if (conv_id is not None and conv_id != mapped_conv) or existing["payload_hash"] != self._payload_hash(mapped_conv,message):
                    raise ConflictError("幂等键已用于不同会话或正文")
                self._interrupt_expired(db,user_id,mapped_conv)
                existing = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,existing["request_id"])).fetchone()
                receipt = self._receipt(db,existing,True); db.commit(); return receipt
            conv_id = conv_id or str(uuid.uuid4())
            session = self._ensure_session(db,user_id,conv_id)
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            if session["deleted_at"]:
                raise ConflictError("会话已删除，请使用新的 conv_id")
            if scope["migration_state"] == "IMPORTING":
                raise ConflictError("记忆迁移中，暂时不能创建新轮次")
            active = db.execute("SELECT 1 FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=? AND julianday(lease_until)>julianday('now')", (self.namespace,user_id,conv_id)).fetchone()
            if active:
                raise LeaseError("会话正在由另一个请求处理")
            self._interrupt_expired(db,user_id,conv_id)
            session = db.execute("SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone()
            token = f"{session['lease_generation']+1}:{secrets.token_urlsafe(24)}"
            now = self._db_now(db); until = self._db_now(db,max(1,lease_seconds))
            eligible = int(self.long_term_enabled and bool(scope["long_term_enabled"]) and user_id.strip().casefold() not in {"anonymous","demo"})
            db.execute("""INSERT INTO memory_turn(namespace,user_id,request_id,idempotency_key,conv_id,turn_seq,source_seq,user_text,status,payload_hash,lease_token,received_at,memory_eligible)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (self.namespace,user_id,request_id,idempotency_key,conv_id,session["next_turn_seq"],scope["next_source_seq"],message,"RECEIVED",self._payload_hash(conv_id,message),token,now,eligible))
            db.execute("UPDATE memory_scope SET next_source_seq=next_source_seq+1,updated_at=? WHERE namespace=? AND user_id=?", (now,self.namespace,user_id))
            db.execute("UPDATE memory_session SET next_turn_seq=next_turn_seq+1,lease_generation=lease_generation+1,lease_owner=?,lease_token=?,lease_until=? WHERE namespace=? AND user_id=? AND conv_id=?", (request_id,token,until,self.namespace,user_id,conv_id))
            row = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            receipt = self._receipt(db,row); db.commit(); return receipt

    def _valid_lease(self, db: sqlite3.Connection, user_id: str, conv_id: str, request_id: str, lease_token: str) -> sqlite3.Row:
        row = db.execute("""SELECT s.* FROM memory_session s JOIN memory_turn t ON t.namespace=s.namespace AND t.user_id=s.user_id AND t.conv_id=s.conv_id
                            WHERE s.namespace=? AND s.user_id=? AND s.conv_id=? AND s.lease_owner=? AND s.lease_token=? AND t.request_id=? AND t.lease_token=? AND t.status='RECEIVED' AND s.deleted_at IS NULL AND t.deleted_at IS NULL AND julianday(s.lease_until)>julianday('now')""", (self.namespace,user_id,conv_id,request_id,lease_token,request_id,lease_token)).fetchone()
        if not row:
            raise LeaseError("会话租约无效或已过期")
        return row

    def renew_turn(self, user_id: str, conv_id: str, request_id: str, lease_token: str, lease_seconds: int = 90) -> str:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._valid_lease(db,user_id,conv_id,request_id,lease_token)
            until = self._db_now(db,max(1,lease_seconds))
            db.execute("UPDATE memory_session SET lease_until=? WHERE namespace=? AND user_id=? AND conv_id=?", (until,self.namespace,user_id,conv_id))
            db.commit(); return until

    def finish_turn(self, user_id: str, conv_id: str, request_id: str, lease_token: str, status: str = "COMPLETED", response: Optional[str] = None) -> TurnReceipt:
        if status not in {"COMPLETED","FAILED","CANCELLED","INTERRUPTED"}:
            raise ValueError("非法轮次终态")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            if not row or row["conv_id"] != conv_id:
                raise ConflictError("未找到请求轮次")
            if row["status"] != "RECEIVED":
                receipt = self._receipt(db,row,True); db.commit(); return receipt
            session = self._valid_lease(db,user_id,conv_id,request_id,lease_token)
            db.execute("UPDATE memory_turn SET status=?,assistant_text=?,finished_at=?,terminal_context_revision=? WHERE namespace=? AND user_id=? AND request_id=?", (status,response if status == "COMPLETED" else None,self._db_now(db),session["context_revision"]+1,self.namespace,user_id,request_id))
            db.execute("UPDATE memory_session SET context_revision=context_revision+1,lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id))
            self._enqueue_turn(db,row)
            row = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            receipt = self._receipt(db,row); db.commit(); return receipt

    def get_snapshot(self, user_id: str, conv_id: str, max_recent: int = 20, max_facts: int = 50, exclude_request_id: Optional[str] = None, candidate_ids: Optional[List[str]] = None) -> MemorySnapshot:
        with self._connection() as db:
            db.execute("BEGIN")
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            session = db.execute("SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone()
            if not scope or not session or session["deleted_at"]:
                return MemorySnapshot(2,"ready",self.namespace,user_id,conv_id,0,0,0,0,{},[],[],[],"")
            floor = int(scope["context_floor_source_seq"])
            summary_row = self._summary_baseline(db, session, scope)
            covered = summary_row["covered_through_turn"] if summary_row else 0
            # RECEIVED 轮次属于当前尚未完成的请求：原文已经持久化，但不能
            # 再注入 prompt，避免 agent.prompt() 看到当前 user 两次。
            turns = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=? AND source_seq>? AND turn_seq>? AND status IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED') AND deleted_at IS NULL AND (? IS NULL OR request_id!=?) ORDER BY turn_seq DESC LIMIT ?", (self.namespace,user_id,conv_id,floor,covered,exclude_request_id,exclude_request_id,max_recent+1)).fetchall()
            context_truncated = len(turns) > max_recent
            turns = turns[:max_recent]
            messages: List[Dict[str, Any]] = []
            for turn in reversed(turns):
                messages.append({"role":"user","content":turn["user_text"],"source_seq":turn["source_seq"],"turn_seq":turn["turn_seq"],"request_id":turn["request_id"]})
                if turn["status"] == "COMPLETED" and turn["assistant_text"] is not None:
                    messages.append({"role":"assistant","content":turn["assistant_text"],"turn_seq":turn["turn_seq"]})
            facts = [] if not self.long_term_enabled or not scope["long_term_enabled"] or user_id.strip().casefold() in {"anonymous","demo"} else [dict(x) for x in db.execute("SELECT fact_id,category,fact_key,content,revision FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE' AND erase_epoch<=? ORDER BY revision DESC LIMIT ?", (self.namespace,user_id,scope["erase_epoch"],max_facts)).fetchall()]
            summary: Dict[str, Any] | str = {}
            if summary_row:
                summary = json.loads(summary_row["content"])
            routing = ""
            if isinstance(summary, dict):
                routing = "；".join(str(summary.get(k,"")) for k in ("user_goal","open_questions","next_step") if summary.get(k))
            episodes = validate_episode_candidates(db, self.namespace, user_id, conv_id, candidate_ids or [],
                recent_turn_seqs=[turn["turn_seq"] for turn in turns], summary_covered_through=covered) if self.long_term_enabled else []
            return MemorySnapshot(2,"ready",self.namespace,user_id,conv_id,int(scope["fact_revision"]),int(session["context_revision"]),int(scope["erase_epoch"]),int(session["summary_generation"]),summary,facts,episodes,messages,routing,context_truncated=context_truncated)

    def list_facts(self, user_id: str) -> Dict[str, Any]:
        with self._connection() as db:
            scope = db.execute("SELECT fact_revision,erase_epoch,long_term_enabled FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            if not scope: return {"facts": [], "revision": 0, "enabled": self.long_term_enabled}
            if user_id.strip().casefold() in {"anonymous", "demo"}:
                return {"facts": [], "revision": scope["fact_revision"], "enabled": False}
            facts = [dict(x) for x in db.execute("SELECT fact_id,category,fact_key,content,origin,status,provenance_state,revision FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE' ORDER BY revision", (self.namespace,user_id)).fetchall()]
            for fact in facts:
                fact["sources"] = [dict(row) for row in db.execute(
                    "SELECT fs.source_seq,t.conv_id FROM memory_fact_source fs JOIN memory_turn t ON fs.namespace=t.namespace AND fs.user_id=t.user_id AND fs.source_seq=t.source_seq WHERE fs.namespace=? AND fs.user_id=? AND fs.fact_id=? AND t.deleted_at IS NULL ORDER BY fs.source_seq",
                    (self.namespace, user_id, fact["fact_id"])).fetchall()]
            return {"facts": facts, "revision": scope["fact_revision"], "erase_epoch": scope["erase_epoch"], "enabled": self.long_term_enabled and bool(scope["long_term_enabled"])}

    def add_fact(self, user_id: str, category: str, content: str, fact_key: Optional[str] = None, source_seqs: Optional[List[int]] = None, evidence: str = "", origin: str = "EXPLICIT") -> Dict[str, Any]:
        if category not in {"preference", "stable_context"}: raise ValueError("不支持的事实类别")
        if not isinstance(content, str) or not content.strip(): raise ValueError("事实内容不能为空")
        if len(content) > 300: raise CapacityError("事实超过单条字符上限")
        normalized = " ".join(content.split()).casefold(); digest = hashlib.sha256(normalized.encode()).hexdigest()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); self._ensure_scope(db,user_id)
            old = db.execute("SELECT fact_id,revision FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE' AND (normalized_hash=? OR (fact_key IS NOT NULL AND fact_key=?))", (self.namespace,user_id,digest,fact_key)).fetchone()
            if old:
                db.commit(); return {"status":"NOOP","fact_id":old["fact_id"],"revision":old["revision"],"mutated":False}
            count = db.execute("SELECT COUNT(*) FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE'", (self.namespace,user_id)).fetchone()[0]
            if count >= 50: db.rollback(); raise CapacityError("长期事实已达到容量上限")
            rev = db.execute("SELECT fact_revision+1 FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()[0]
            fact_id = f"fact-{secrets.token_hex(10)}"
            db.execute("INSERT INTO memory_fact(namespace,user_id,fact_id,category,fact_key,content,normalized_hash,origin,status,valid_from,revision,erase_epoch) SELECT ?,?,?,?,?,?,?,?,?,?,?,erase_epoch FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id,fact_id,category,fact_key,content,digest,origin,"ACTIVE",utc_now(),rev,self.namespace,user_id))
            for source in source_seqs or []: db.execute("INSERT OR IGNORE INTO memory_fact_source VALUES(?,?,?,?,?)", (self.namespace,user_id,fact_id,int(source),evidence[:500]))
            db.execute("UPDATE memory_scope SET fact_revision=?,updated_at=? WHERE namespace=? AND user_id=?", (rev,utc_now(),self.namespace,user_id)); db.commit()
            return {"status":"WRITTEN","fact_id":fact_id,"revision":rev,"mutated":True}

    def flush_user_messages(self, user_id: str, max_messages: int = 20) -> Dict[str, Any]:
        """Do not interpret keywords as confirmed facts or advance an unjudged cursor.

        The model Judge is not wired yet. Recovery can enqueue terminal user input,
        but explicit callers must receive an honest pending outcome.
        """
        with self._connection() as db:
            db.execute("BEGIN")
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            enabled = self.long_term_enabled and bool(scope and scope["long_term_enabled"]) and user_id.strip().casefold() not in {"anonymous","demo"}
            return {"status":"PENDING" if enabled else "DISABLED", "covered_source_seq":scope["extract_cursor"] if scope else 0,"revision":scope["fact_revision"] if scope else 0,"applied":0,"mutated":False,"queued":False}

    def _operation_replay(self, db: sqlite3.Connection, user_id: str, request_id: Optional[str], operation: str) -> Optional[Dict[str,Any]]:
        if not request_id:
            return None
        row = db.execute("SELECT kind,result FROM memory_job WHERE namespace=? AND user_id=? AND dedup_key=?", (self.namespace,user_id,f"operation:{request_id}")).fetchone()
        if not row:
            return None
        if row["kind"] != operation:
            raise ConflictError("记忆操作 request_id 已用于另一操作")
        return json.loads(row["result"])

    def _audit_operation(self, db: sqlite3.Connection, user_id: str, request_id: Optional[str], operation: str, result: Dict[str,Any]) -> None:
        now = self._db_now(db)
        db.execute("INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,status,next_run_at,result,created_at) VALUES(?,?,?,?,?,'WRITTEN',?,?,?)", (self.namespace,user_id,secrets.token_hex(16),operation,f"operation:{request_id or secrets.token_hex(16)}",now,json.dumps(result),now))

    def _preserve_inflight_index_jobs(self, db: sqlite3.Connection, user_id: str) -> None:
        """Keep body-free per-claim receipts until a late vector call acknowledges.

        An expired database lease cannot prove that a remote operation stopped.
        Separate receipts retain all attempts even when the main job is reclaimed.
        """
        rows=db.execute("SELECT * FROM memory_job WHERE namespace=? AND user_id=? AND kind IN ('index_upsert','index_write_receipt') AND status='PROCESSING' AND claim_token IS NOT NULL",(self.namespace,user_id)).fetchall()
        for row in rows:
            try: envelope=json.loads(row['result'] or '{}')
            except (TypeError,ValueError): envelope={}
            document_id=envelope.get('document_id')
            if not document_id and row['kind']=='index_upsert':
                episode=db.execute("SELECT * FROM memory_episode WHERE namespace=? AND user_id=? AND episode_id=?",(self.namespace,user_id,envelope.get('episode_id',''))).fetchone()
                if episode:
                    from .episodic_index import episode_document_id
                    document_id=episode_document_id(self.namespace,user_id,episode['conv_id'],episode['from_turn'],episode['to_turn'],episode['index_version'])
            metadata={'document_id':document_id} if document_id else {}
            db.execute("UPDATE memory_job SET status='CANCELLED',error_code='INDEX_WRITE_UNACKNOWLEDGED',result=? WHERE namespace=? AND job_id=?",(json.dumps(metadata),self.namespace,row['job_id']))

    def _erase_barrier(self, db: sqlite3.Connection, user_id: str, reason: str) -> None:
        """Fence stale workers and every subsequent context read in one commit."""
        now = self._db_now(db)
        self._preserve_inflight_index_jobs(db,user_id)
        db.execute("UPDATE memory_scope SET erase_epoch=erase_epoch+1,context_floor_source_seq=next_source_seq-1,extract_cursor=MAX(extract_cursor,next_source_seq-1),updated_at=? WHERE namespace=? AND user_id=?", (now,self.namespace,user_id))
        db.execute("UPDATE memory_session SET current_summary='',summary_generation=0,context_revision=context_revision+1 WHERE namespace=? AND user_id=?", (self.namespace,user_id))
        db.execute("UPDATE memory_episode SET invalid_at=? WHERE namespace=? AND user_id=? AND invalid_at IS NULL", (now,self.namespace,user_id))
        db.execute("UPDATE memory_job SET status='CANCELLED',claim_token=NULL,lease_until=NULL,error_code=? WHERE namespace=? AND user_id=? AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED') AND kind NOT IN ('index_delete','index_write_receipt')", (reason,self.namespace,user_id))
        epoch = db.execute("SELECT erase_epoch FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO memory_job(namespace,user_id,job_id,kind,dedup_key,expected_epoch,next_run_at,created_at) VALUES(?,?,?,?,?,?,?,?)", (self.namespace,user_id,secrets.token_hex(16),'index_delete',f'forget-before-epoch:{epoch}',epoch,now,now))

    def retract_fact(self, user_id: str, fact_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = f"retract:{fact_id}"
            replay = self._operation_replay(db,user_id,request_id,operation)
            if replay is not None:
                db.commit(); return replay
            row = db.execute("SELECT status FROM memory_fact WHERE namespace=? AND user_id=? AND fact_id=?", (self.namespace,user_id,fact_id)).fetchone()
            if not row:
                raise ConflictError("事实不存在")
            mutated = row["status"] == "ACTIVE"
            db.execute("UPDATE memory_scope SET fact_revision=fact_revision+? WHERE namespace=? AND user_id=?", (int(mutated),self.namespace,user_id))
            rev = db.execute("SELECT fact_revision FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()[0]
            db.execute("UPDATE memory_fact SET status='INVALID',invalid_at=COALESCE(invalid_at,?),revision=? WHERE namespace=? AND user_id=? AND fact_id=?", (self._db_now(db),rev,self.namespace,user_id,fact_id))
            self._erase_barrier(db,user_id,"FACT_RETRACTED")
            result = {"status":"WRITTEN","revision":rev,"mutated":True,"applied":int(mutated)}
            self._audit_operation(db,user_id,request_id,operation,result)
            db.commit(); return result

    def clear_facts(self, user_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); self._ensure_scope(db,user_id)
            replay = self._operation_replay(db,user_id,request_id,"clear")
            if replay is not None:
                db.commit(); return replay
            db.execute("UPDATE memory_scope SET fact_revision=fact_revision+1 WHERE namespace=? AND user_id=?", (self.namespace,user_id))
            rev = db.execute("SELECT fact_revision FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()[0]
            changed = db.execute("UPDATE memory_fact SET status='INVALID',invalid_at=?,revision=? WHERE namespace=? AND user_id=? AND status='ACTIVE'", (self._db_now(db),rev,self.namespace,user_id)).rowcount
            self._erase_barrier(db,user_id,"FACTS_CLEARED")
            result = {"status":"WRITTEN","revision":rev,"mutated":True,"applied":changed}
            self._audit_operation(db,user_id,request_id,"clear",result)
            db.commit(); return result

    def set_long_term_enabled(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); self._ensure_scope(db,user_id)
            previous = db.execute("SELECT long_term_enabled FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()[0]
            if not enabled and previous:
                self._preserve_inflight_index_jobs(db,user_id)
                # Keep conversation context, but fence all prior extraction material.
                db.execute("UPDATE memory_scope SET erase_epoch=erase_epoch+1,extract_cursor=next_source_seq-1 WHERE namespace=? AND user_id=?", (self.namespace,user_id))
                db.execute("UPDATE memory_turn SET memory_eligible=0 WHERE namespace=? AND user_id=? AND source_seq>(SELECT extract_cursor FROM memory_scope WHERE namespace=? AND user_id=?)", (self.namespace,user_id,self.namespace,user_id))
                db.execute("UPDATE memory_job SET status='CANCELLED',claim_token=NULL,lease_until=NULL,error_code='LONG_TERM_DISABLED' WHERE namespace=? AND user_id=? AND kind IN ('fact_extract','index_upsert') AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED')", (self.namespace,user_id))
            db.execute("UPDATE memory_scope SET long_term_enabled=?,updated_at=? WHERE namespace=? AND user_id=?", (int(enabled),self._db_now(db),self.namespace,user_id))
            db.commit(); return {"status":"WRITTEN","enabled":enabled}

    def delete_conversation(self, user_id: str, conv_id: str) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT deleted_at FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone()
            if not row:
                raise ConflictError("会话不存在")
            if row["deleted_at"]:
                db.commit(); return {"status":"WRITTEN","conv_id":conv_id}
            now = self._db_now(db)
            db.execute("UPDATE memory_fact SET provenance_state='SOURCE_DELETED' WHERE namespace=? AND user_id=? AND fact_id IN (SELECT fs.fact_id FROM memory_fact_source fs JOIN memory_turn t ON fs.namespace=t.namespace AND fs.user_id=t.user_id AND fs.source_seq=t.source_seq WHERE t.namespace=? AND t.user_id=? AND t.conv_id=?)", (self.namespace,user_id,self.namespace,user_id,conv_id))
            db.execute("DELETE FROM memory_fact_source WHERE namespace=? AND user_id=? AND source_seq IN (SELECT source_seq FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=?)", (self.namespace,user_id,self.namespace,user_id,conv_id))
            db.execute("UPDATE memory_turn SET user_text='',assistant_text=NULL,status='DELETED',memory_eligible=0,deleted_at=?,finished_at=COALESCE(finished_at,?) WHERE namespace=? AND user_id=? AND conv_id=?", (now,now,self.namespace,user_id,conv_id))
            db.execute("DELETE FROM memory_summary WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id))
            db.execute("DELETE FROM memory_episode WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id))
            self._erase_barrier(db,user_id,"CONVERSATION_DELETED")
            db.execute("UPDATE memory_session SET deleted_at=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE namespace=? AND user_id=? AND conv_id=?", (now,self.namespace,user_id,conv_id))
            db.execute("UPDATE memory_job SET result=NULL WHERE namespace=? AND user_id=? AND status='CANCELLED' AND COALESCE(error_code,'')!='INDEX_WRITE_UNACKNOWLEDGED'", (self.namespace,user_id))
            db.commit(); return {"status":"WRITTEN","conv_id":conv_id}

    def delete_all(self, user_id: str) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); self._ensure_scope(db,user_id)
            self._erase_barrier(db,user_id,"ALL_MEMORY_DELETED")
            now = self._db_now(db)
            for table in ("memory_fact_source","memory_fact","memory_episode","memory_summary"):
                db.execute(f"DELETE FROM {table} WHERE namespace=? AND user_id=?", (self.namespace,user_id))
            db.execute("DELETE FROM memory_job WHERE namespace=? AND user_id=? AND COALESCE(error_code,'')!='INDEX_WRITE_UNACKNOWLEDGED'",(self.namespace,user_id))
            # Retain only body-free request/session tombstones to fence old retries.
            db.execute("UPDATE memory_turn SET user_text='',assistant_text=NULL,status='DELETED',memory_eligible=0,deleted_at=?,finished_at=COALESCE(finished_at,?) WHERE namespace=? AND user_id=?", (now,now,self.namespace,user_id))
            db.execute("UPDATE memory_session SET deleted_at=?,current_summary='',lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE namespace=? AND user_id=?", (now,self.namespace,user_id))
            db.execute("UPDATE memory_scope SET fact_revision=fact_revision+1,migration_state='ERASED' WHERE namespace=? AND user_id=?", (self.namespace,user_id))
            # Cleanup is honest durable work even before the vector worker is enabled.
            epoch = db.execute("SELECT erase_epoch FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()[0]
            db.execute("INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,expected_epoch,next_run_at,created_at) VALUES(?,?,?,?,?,?,?,?)", (self.namespace,user_id,secrets.token_hex(16),'index_delete',f'delete-all:{epoch}',epoch,now,now))
            db.commit(); return {"status":"WRITTEN","user_id":user_id,"index_cleanup":"PENDING"}

    def claim_due_jobs(self, limit: int = 10, lease_seconds: int = 180) -> List[Dict[str, Any]]:
        """领取到期任务；PROCESSING 超时后可被新 token 重新领取。"""
        claimed: List[Dict[str, Any]] = []
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self._db_now(db); until = self._db_now(db,max(1,lease_seconds))
            rows = db.execute("SELECT * FROM memory_job WHERE namespace=? AND status IN ('READY','RETRY_WAIT','PROCESSING') AND next_run_at<=? AND (status!='PROCESSING' OR lease_until IS NULL OR lease_until<=?) ORDER BY created_at LIMIT ?", (self.namespace,now,now,max(1,limit))).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(18)
                db.execute("UPDATE memory_job SET status='PROCESSING',claim_token=?,lease_until=?,attempt_count=attempt_count+1 WHERE namespace=? AND user_id=? AND job_id=?", (token,until,self.namespace,row["user_id"],row["job_id"]))
                item = dict(row); item.update({"claim_token": token, "lease_until": until, "attempt_count": row["attempt_count"] + 1, "status": "PROCESSING"}); claimed.append(item)
            db.commit()
        return claimed

    def recover_interrupted_turns(self) -> int:
        """Recover user input without inventing an assistant success."""
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = self._interrupt_expired(db)
            db.commit(); return changed

    def complete_job(self, job_id: str, claim_token: str, result: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE memory_job SET status='WRITTEN',result=?,lease_until=NULL WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')", (json.dumps(result or {}, ensure_ascii=False),self.namespace,job_id,claim_token)).rowcount
            db.commit(); return changed == 1

    def retry_job(self, job_id: str, claim_token: str, error: str, max_attempts: int = 3) -> str:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT attempt_count FROM memory_job WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')", (self.namespace,job_id,claim_token)).fetchone()
            if not row: db.rollback(); return "STALE"
            status = "QUARANTINED" if row["attempt_count"] >= max_attempts else "RETRY_WAIT"
            db.execute("UPDATE memory_job SET status=?,error_code=?,next_run_at=?,lease_until=NULL WHERE namespace=? AND job_id=? AND claim_token=?", (status,error[:200],self._db_now(db,min(300,2 ** row["attempt_count"])),self.namespace,job_id,claim_token)); db.commit(); return status

    def close(self) -> None:
        return None

