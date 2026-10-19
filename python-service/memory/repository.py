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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .models import MemoryScope, MemorySnapshot, TurnReceipt


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


class MemoryRepository:
    SCHEMA_VERSION = 1

    def __init__(self, path: str = "data/memory/memory.sqlite3", namespace: str = "lumenmind"):
        self.path = str(Path(path).expanduser())
        self.namespace = namespace.strip() or "lumenmind"
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
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_schema_version(version INTEGER NOT NULL);
                INSERT INTO memory_schema_version(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM memory_schema_version);
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
                """
            )

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

    def begin_turn(
        self, user_id: str, conv_id: str, request_id: str, message: str,
        idempotency_key: Optional[str] = None, lease_seconds: int = 90,
    ) -> TurnReceipt:
        if not user_id.strip() or not conv_id.strip() or not request_id.strip():
            raise ValueError("user_id、conv_id、request_id 不能为空")
        payload_hash = self._payload_hash(conv_id, message)
        now = datetime.now(timezone.utc)
        until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?",
                (self.namespace, user_id, request_id),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash or existing["conv_id"] != conv_id:
                    db.rollback()
                    raise ConflictError("request_id 已用于不同会话或正文")
                session = self._ensure_session(db, user_id, conv_id)
                db.commit()
                return TurnReceipt(request_id, self.namespace, user_id, conv_id, existing["turn_seq"], existing["source_seq"], existing["status"], existing["lease_token"] or "", session["context_revision"], lease_until=session["lease_until"])
            if idempotency_key:
                idem = db.execute(
                    "SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND idempotency_key=?",
                    (self.namespace, user_id, idempotency_key),
                ).fetchone()
                if idem:
                    if idem["payload_hash"] != payload_hash or idem["conv_id"] != conv_id:
                        db.rollback()
                        raise ConflictError("Idempotency-Key 已用于不同会话或正文")
                    db.commit()
                    session = self._ensure_session(db, user_id, conv_id)
                    return TurnReceipt(idem["request_id"], self.namespace, user_id, conv_id, idem["turn_seq"], idem["source_seq"], idem["status"], idem["lease_token"] or "", int(session["context_revision"]), lease_until=session["lease_until"])
            session = self._ensure_session(db, user_id, conv_id)
            if session["deleted_at"]:
                db.rollback()
                raise ConflictError("会话已删除，请使用新的 conv_id")
            if session["lease_until"] and session["lease_until"] > now.isoformat():
                db.rollback()
                raise LeaseError("会话正在由另一个请求处理")
            scope = db.execute(
                "SELECT next_source_seq FROM memory_scope WHERE namespace=? AND user_id=?",
                (self.namespace, user_id),
            ).fetchone()
            source_seq = int(scope[0])
            turn_seq = int(session["next_turn_seq"])
            token = secrets.token_urlsafe(24)
            db.execute(
                """INSERT INTO memory_turn(namespace,user_id,request_id,idempotency_key,conv_id,turn_seq,source_seq,user_text,status,payload_hash,lease_token,received_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.namespace, user_id, request_id, idempotency_key, conv_id, turn_seq, source_seq, message, "RECEIVED", payload_hash, token, now.isoformat()),
            )
            db.execute("UPDATE memory_scope SET next_source_seq=next_source_seq+1,updated_at=? WHERE namespace=? AND user_id=?", (now.isoformat(), self.namespace, user_id))
            db.execute("UPDATE memory_session SET next_turn_seq=next_turn_seq+1,lease_owner=?,lease_token=?,lease_until=? WHERE namespace=? AND user_id=? AND conv_id=?", (request_id, token, until, self.namespace, user_id, conv_id))
            db.commit()
            return TurnReceipt(request_id, self.namespace, user_id, conv_id, turn_seq, source_seq, "RECEIVED", token, int(session["context_revision"]), lease_until=until)

    def renew_turn(self, user_id: str, conv_id: str, request_id: str, lease_token: str, lease_seconds: int = 90) -> str:
        until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT lease_token,lease_until FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone()
            if not row or row["lease_token"] != lease_token or row["lease_until"] <= utc_now():
                db.rollback(); raise LeaseError("会话租约无效或已过期")
            turn = db.execute("SELECT status FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            if not turn or turn["status"] != "RECEIVED":
                db.rollback(); raise LeaseError("轮次已结束")
            db.execute("UPDATE memory_session SET lease_until=? WHERE namespace=? AND user_id=? AND conv_id=?", (until,self.namespace,user_id,conv_id)); db.commit(); return until

    def finish_turn(self, user_id: str, conv_id: str, request_id: str, lease_token: str, status: str = "COMPLETED", response: Optional[str] = None) -> TurnReceipt:
        if status not in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            raise ValueError("非法轮次终态")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            if not row or row["conv_id"] != conv_id:
                db.rollback(); raise ConflictError("未找到请求轮次")
            if row["status"] != "RECEIVED":
                db.commit(); return TurnReceipt(request_id,self.namespace,user_id,conv_id,row["turn_seq"],row["source_seq"],row["status"],row["lease_token"] or "",0,response=row["assistant_text"])
            session = db.execute("SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone()
            if not session or session["lease_token"] != lease_token or session["lease_until"] <= utc_now():
                db.rollback(); raise LeaseError("会话租约无效或已过期")
            finished = utc_now()
            db.execute("UPDATE memory_turn SET status=?,assistant_text=?,finished_at=? WHERE namespace=? AND user_id=? AND request_id=?", (status,response,self._now_or_none(finished),self.namespace,user_id,request_id))
            db.execute("UPDATE memory_session SET context_revision=context_revision+1,lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id))
            scope = db.execute("SELECT fact_revision,extract_cursor,erase_epoch FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            db.execute(
                """INSERT OR IGNORE INTO memory_job(
                    namespace,user_id,job_id,kind,dedup_key,from_seq,to_seq,
                    expected_revision,expected_cursor,expected_epoch,next_run_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.namespace, user_id, secrets.token_hex(16), "fact_extract",
                 f"fact:{self.namespace}:{user_id}:{row['source_seq']}", row["source_seq"], row["source_seq"],
                 scope["fact_revision"], scope["extract_cursor"], scope["erase_epoch"], utc_now(), utc_now()),
            )
            db.commit()
            return TurnReceipt(request_id,self.namespace,user_id,conv_id,row["turn_seq"],row["source_seq"],status,row["lease_token"] or "",int(session["context_revision"])+1,response=response)

    @staticmethod
    def _now_or_none(value: str) -> str:
        return value

    def get_snapshot(self, user_id: str, conv_id: str, max_recent: int = 20, max_facts: int = 50) -> MemorySnapshot:
        with self._connection() as db:
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            session = db.execute("SELECT * FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone()
            if not scope or not session or session["deleted_at"]:
                return MemorySnapshot(2,"ready",self.namespace,user_id,conv_id,0,0,0,0,{},[],[],[],"")
            floor = int(scope["context_floor_source_seq"])
            # RECEIVED 轮次属于当前尚未完成的请求：原文已经持久化，但不能
            # 再注入 prompt，避免 agent.prompt() 看到当前 user 两次。
            turns = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND conv_id=? AND source_seq>? AND status='COMPLETED' ORDER BY turn_seq DESC LIMIT ?", (self.namespace,user_id,conv_id,floor,max_recent)).fetchall()
            messages: List[Dict[str, Any]] = []
            for turn in reversed(turns):
                messages.append({"role":"user","content":turn["user_text"],"source_seq":turn["source_seq"],"turn_seq":turn["turn_seq"],"request_id":turn["request_id"]})
                if turn["assistant_text"] is not None:
                    messages.append({"role":"assistant","content":turn["assistant_text"],"turn_seq":turn["turn_seq"]})
            facts = [] if not scope["long_term_enabled"] else [dict(x) for x in db.execute("SELECT fact_id,category,fact_key,content,revision FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE' AND erase_epoch<=? ORDER BY revision DESC LIMIT ?", (self.namespace,user_id,scope["erase_epoch"],max_facts)).fetchall()]
            summary: Dict[str, Any] | str = {}
            if session["current_summary"]:
                try: summary = json.loads(session["current_summary"])
                except json.JSONDecodeError: summary = session["current_summary"]
            routing = ""
            if isinstance(summary, dict):
                routing = "；".join(str(summary.get(k,"")) for k in ("user_goal","open_questions","next_step") if summary.get(k))
            return MemorySnapshot(2,"ready",self.namespace,user_id,conv_id,int(scope["fact_revision"]),int(session["context_revision"]),int(scope["erase_epoch"]),int(session["summary_generation"]),summary,facts,[],messages,routing)

    def list_facts(self, user_id: str) -> Dict[str, Any]:
        with self._connection() as db:
            scope = db.execute("SELECT fact_revision,erase_epoch,long_term_enabled FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            if not scope: return {"facts": [], "revision": 0, "enabled": True}
            facts = [dict(x) for x in db.execute("SELECT fact_id,category,fact_key,content,origin,status,provenance_state,revision FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE' ORDER BY revision", (self.namespace,user_id)).fetchall()]
            return {"facts": facts, "revision": scope["fact_revision"], "erase_epoch": scope["erase_epoch"], "enabled": bool(scope["long_term_enabled"])}

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
        """处理明确的“记住”偏好；游标与事实在同一事务推进。

        这是首期无模型环境的保守 judge：无法识别的 user 素材结算为 NOOP，
        助手消息永远不进入素材集合。生产环境可用同一事务协议替换 Judge。
        """
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone()
            if not scope or not scope["long_term_enabled"]:
                db.rollback(); return {"status":"DISABLED","covered_source_seq":scope["extract_cursor"] if scope else 0,"revision":scope["fact_revision"] if scope else 0,"applied":0,"mutated":False}
            rows = db.execute("SELECT source_seq,user_text FROM memory_turn WHERE namespace=? AND user_id=? AND source_seq>? AND memory_eligible=1 ORDER BY source_seq LIMIT ?", (self.namespace,user_id,scope["extract_cursor"],max(1,max_messages))).fetchall()
            if not rows:
                db.commit(); return {"status":"NOOP","covered_source_seq":scope["extract_cursor"],"revision":scope["fact_revision"],"applied":0,"mutated":False}
            revision = scope["fact_revision"]; applied=0; covered = scope["extract_cursor"]
            for row in rows:
                covered = max(covered, row["source_seq"]); text = row["user_text"]
                match = re.search(r"(?:记住|记下来|以后|今后).*?(邮件|电话|短信|简短|详细)", text, re.I)
                if not match: continue
                token = match.group(1)
                if token in {"邮件","电话","短信"}: key, category, content = "contact.channel", "preference", f"优先通过{token}联系"
                else: key, category, content = "response.detail", "preference", f"解释时尽量{token}"
                digest = hashlib.sha256(" ".join(content.split()).casefold().encode()).hexdigest()
                exists = db.execute("SELECT fact_id FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE' AND (normalized_hash=? OR fact_key=?)", (self.namespace,user_id,digest,key)).fetchone()
                if exists: continue
                count = db.execute("SELECT COUNT(*) FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE'", (self.namespace,user_id)).fetchone()[0]
                if count >= 50: db.rollback(); return {"status":"CAPACITY_EXCEEDED","covered_source_seq":scope["extract_cursor"],"revision":scope["fact_revision"],"applied":0,"mutated":False}
                revision += 1; fact_id=f"fact-{secrets.token_hex(10)}"
                db.execute("INSERT INTO memory_fact(namespace,user_id,fact_id,category,fact_key,content,normalized_hash,origin,status,valid_from,revision,erase_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (self.namespace,user_id,fact_id,category,key,content,digest,"EXPLICIT","ACTIVE",utc_now(),revision,scope["erase_epoch"]))
                db.execute("INSERT INTO memory_fact_source VALUES(?,?,?,?,?)", (self.namespace,user_id,fact_id,row["source_seq"],text[:500])); applied += 1
            db.execute("UPDATE memory_scope SET extract_cursor=?,fact_revision=?,updated_at=? WHERE namespace=? AND user_id=?", (covered,revision,utc_now(),self.namespace,user_id)); db.commit()
            return {"status":"WRITTEN" if applied else "NOOP","covered_source_seq":covered,"revision":revision,"applied":applied,"mutated":bool(applied)}

    def retract_fact(self, user_id: str, fact_id: str) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT status FROM memory_fact WHERE namespace=? AND user_id=? AND fact_id=?", (self.namespace,user_id,fact_id)).fetchone()
            if not row: db.rollback(); raise ConflictError("事实不存在")
            scope = db.execute("SELECT fact_revision,erase_epoch,next_source_seq FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone(); rev=scope["fact_revision"]+1
            db.execute("UPDATE memory_fact SET status='INVALID',invalid_at=?,revision=? WHERE namespace=? AND user_id=? AND fact_id=?", (utc_now(),rev,self.namespace,user_id,fact_id))
            db.execute("UPDATE memory_episode SET invalid_at=? WHERE namespace=? AND user_id=? AND invalid_at IS NULL", (utc_now(),self.namespace,user_id))
            db.execute("UPDATE memory_scope SET fact_revision=?,erase_epoch=erase_epoch+1,context_floor_source_seq=next_source_seq-1,extract_cursor=MAX(extract_cursor,next_source_seq-1),updated_at=? WHERE namespace=? AND user_id=?", (rev,utc_now(),self.namespace,user_id)); db.commit()
            return {"status":"WRITTEN","revision":rev,"mutated":row["status"]=="ACTIVE"}

    def clear_facts(self, user_id: str) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); self._ensure_scope(db,user_id); scope=db.execute("SELECT fact_revision,next_source_seq FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)).fetchone(); rev=scope["fact_revision"]+1
            db.execute("UPDATE memory_fact SET status='INVALID',invalid_at=?,revision=? WHERE namespace=? AND user_id=? AND status='ACTIVE'", (utc_now(),rev,self.namespace,user_id)); db.execute("UPDATE memory_episode SET invalid_at=? WHERE namespace=? AND user_id=? AND invalid_at IS NULL", (utc_now(),self.namespace,user_id)); db.execute("UPDATE memory_scope SET fact_revision=?,erase_epoch=erase_epoch+1,context_floor_source_seq=next_source_seq-1,extract_cursor=MAX(extract_cursor,next_source_seq-1),updated_at=? WHERE namespace=? AND user_id=?", (rev,utc_now(),self.namespace,user_id)); db.commit(); return {"status":"WRITTEN","revision":rev,"mutated":True}

    def set_long_term_enabled(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); self._ensure_scope(db,user_id); db.execute("UPDATE memory_scope SET long_term_enabled=?,erase_epoch=erase_epoch+?,updated_at=? WHERE namespace=? AND user_id=?", (1 if enabled else 0,0 if enabled else 1,utc_now(),self.namespace,user_id))
            if not enabled:
                db.execute("UPDATE memory_job SET status='CANCELLED',error_code='LONG_TERM_DISABLED' WHERE namespace=? AND user_id=? AND status IN ('READY','PROCESSING','RETRY_WAIT')", (self.namespace,user_id))
            db.commit(); return {"status":"WRITTEN","enabled":enabled}

    def delete_conversation(self, user_id: str, conv_id: str) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT 1 FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)).fetchone();
            if not row: db.rollback(); raise ConflictError("会话不存在")
            db.execute("UPDATE memory_session SET deleted_at=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,context_revision=context_revision+1 WHERE namespace=? AND user_id=? AND conv_id=?", (utc_now(),self.namespace,user_id,conv_id)); db.execute("DELETE FROM memory_summary WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,user_id,conv_id)); db.execute("UPDATE memory_scope SET erase_epoch=erase_epoch+1,updated_at=? WHERE namespace=? AND user_id=?", (utc_now(),self.namespace,user_id)); db.commit(); return {"status":"WRITTEN","conv_id":conv_id}

    def delete_all(self, user_id: str) -> Dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("memory_fact_source","memory_fact","memory_episode","memory_summary","memory_turn","memory_job","memory_session"): db.execute(f"DELETE FROM {table} WHERE namespace=? AND user_id=?", (self.namespace,user_id))
            db.execute("DELETE FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,user_id)); db.commit(); return {"status":"WRITTEN","user_id":user_id,"index_cleanup":"PENDING"}

    def claim_due_jobs(self, limit: int = 10, lease_seconds: int = 180) -> List[Dict[str, Any]]:
        """领取到期任务；PROCESSING 超时后可被新 token 重新领取。"""
        now = utc_now(); until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat(); claimed: List[Dict[str, Any]] = []
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM memory_job WHERE namespace=? AND status IN ('READY','RETRY_WAIT','PROCESSING') AND next_run_at<=? AND (status!='PROCESSING' OR lease_until IS NULL OR lease_until<=?) ORDER BY created_at LIMIT ?", (self.namespace,now,now,max(1,limit))).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(18)
                db.execute("UPDATE memory_job SET status='PROCESSING',claim_token=?,lease_until=?,attempt_count=attempt_count+1 WHERE namespace=? AND user_id=? AND job_id=?", (token,until,self.namespace,row["user_id"],row["job_id"]))
                item = dict(row); item.update({"claim_token": token, "attempt_count": row["attempt_count"] + 1, "status": "PROCESSING"}); claimed.append(item)
            db.commit()
        return claimed

    def recover_interrupted_turns(self) -> int:
        """启动/轮询时把租约已过期且没有 assistant 的轮次标为 INTERRUPTED。"""
        now = utc_now(); changed = 0
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT t.namespace,t.user_id,t.request_id,t.source_seq,s.conv_id FROM memory_turn t JOIN memory_session s ON s.namespace=t.namespace AND s.user_id=t.user_id AND s.conv_id=t.conv_id WHERE t.namespace=? AND t.status='RECEIVED' AND (s.lease_until IS NULL OR s.lease_until<=?)", (self.namespace,now)).fetchall()
            for row in rows:
                changed += db.execute("UPDATE memory_turn SET status='INTERRUPTED',finished_at=? WHERE namespace=? AND user_id=? AND request_id=? AND status='RECEIVED'", (now,row["namespace"],row["user_id"],row["request_id"])).rowcount
                db.execute("UPDATE memory_session SET lease_owner=NULL,lease_token=NULL,lease_until=NULL,context_revision=context_revision+1 WHERE namespace=? AND user_id=? AND conv_id=?", (self.namespace,row["user_id"],row["conv_id"]))
            db.commit()
        return changed

    def complete_job(self, job_id: str, claim_token: str, result: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE memory_job SET status='WRITTEN',result=?,lease_until=NULL WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING'", (json.dumps(result or {}, ensure_ascii=False),self.namespace,job_id,claim_token)).rowcount
            db.commit(); return changed == 1

    def retry_job(self, job_id: str, claim_token: str, error: str, max_attempts: int = 3) -> str:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT attempt_count FROM memory_job WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING'", (self.namespace,job_id,claim_token)).fetchone()
            if not row: db.rollback(); return "STALE"
            status = "QUARANTINED" if row["attempt_count"] >= max_attempts else "RETRY_WAIT"
            db.execute("UPDATE memory_job SET status=?,error_code=?,next_run_at=?,lease_until=NULL WHERE namespace=? AND job_id=? AND claim_token=?", (status,error[:200],utc_now(),self.namespace,job_id,claim_token)); db.commit(); return status

    def close(self) -> None:
        return None

