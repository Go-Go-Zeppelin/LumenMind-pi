"""Offline HTTP contracts for the authoritative SQLite memory service."""
import asyncio
import builtins
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main
from memory.config import MemorySettings
from memory.conversation_memory import MemoryManager
from memory.models import MemorySnapshot


@pytest.fixture
def manager(tmp_path, monkeypatch):
    result = MemoryManager(db_path=str(tmp_path / "memory.sqlite3"))
    monkeypatch.setattr(main, "_memory", result)
    yield result
    asyncio.run(result.close())


@pytest.fixture
def client(manager):
    # Lifespan is covered separately; endpoints require no external store/model.
    return TestClient(main.app)


def begin(client, request_id="r1", **overrides):
    return client.post("/memory/turn/begin", json={
        "user_id": "alice", "request_id": request_id, "message": "请记住以后邮件联系我", **overrides,
    })


def finish(client, receipt, response="好的"):
    return client.post("/memory/turn/finish", json={
        "user_id": "alice", "conv_id": receipt["conv_id"],
        "request_id": receipt["request_id"], "lease_token": receipt["lease_token"],
        "response": response,
    })


def test_missing_conversation_generated_and_completed_replay_includes_response(client):
    started = begin(client)
    assert started.status_code == 200
    first = started.json()
    assert first["conv_id"] and first["lease_until"]
    assert first["replayed"] is False
    assert finish(client, first).status_code == 200
    replay = begin(client).json()
    assert replay["replayed"] is True
    assert replay["conv_id"] == first["conv_id"]
    assert replay["response"] == "好的"
    assert replay["status"] == "COMPLETED"
    assert finish(client, first).json()["replayed"] is True


def test_header_key_replays_original_request_and_conflicting_content_is_409(client):
    payload = {"user_id": "alice", "request_id": "r1", "message": "hello"}
    first = client.post("/memory/turn/begin", json=payload, headers={"Idempotency-Key": "one"})
    assert first.status_code == 200
    second = client.post("/memory/turn/begin", json={**payload, "request_id": "r2"}, headers={"Idempotency-Key": "one"})
    assert second.status_code == 200
    assert second.json()["request_id"] == "r1"
    assert second.json()["conv_id"] == first.json()["conv_id"]
    conflict = client.post("/memory/turn/begin", json={**payload, "message": "changed"})
    assert conflict.status_code == 409
    mismatch = client.post("/memory/turn/begin", json={**payload, "idempotency_key": "two"}, headers={"Idempotency-Key": "one"})
    assert mismatch.status_code == 422


def test_invalid_finish_and_lost_lease_map_to_client_errors(client):
    receipt = begin(client).json()
    bad = {"user_id": "alice", "conv_id": receipt["conv_id"], "request_id": "r1", "lease_token": "other"}
    assert client.post("/memory/turn/finish", json=bad).status_code == 409
    assert client.post("/memory/turn/finish", json={**bad, "status": "pretend"}).status_code == 422
    assert begin(client, message="").status_code == 422


def test_context_reads_once_and_excludes_current_request_from_all_outputs(client, manager, monkeypatch):
    first = begin(client).json()
    assert finish(client, first).status_code == 200
    current = begin(client, "r2", conv_id=first["conv_id"], message="UNIQUE_CURRENT_MESSAGE").json()
    original = manager.get_snapshot
    calls = []

    async def snapshot(*args):
        calls.append(args)
        return await original(*args)

    monkeypatch.setattr(manager, "get_snapshot", snapshot)
    result = client.post("/memory/context", json={"user_id": "alice", "conv_id": first["conv_id"], "request_id": current["request_id"]})
    assert result.status_code == 200
    assert len(calls) == 1
    assert "UNIQUE_CURRENT_MESSAGE" not in result.text
    assert [item["role"] for item in result.json()["recent_messages"]] == ["user", "assistant"]
    assert result.json()["snapshot"]["context_revision"] >= 1


def test_anonymous_snapshot_cannot_expose_cross_session_facts(manager, monkeypatch):
    def snapshot(*args, **kwargs):
        return MemorySnapshot(2, "READY", "lumenmind", "anonymous", "c1", 1, 1, 0, 0,
            facts=[{"content": "secret", "category": "preference"}], episodes=[{"summary": "secret episode"}],
            recent_messages=[{"role": "user", "content": "local"}], routing_context="secret routing")
    monkeypatch.setattr(manager._repo, "get_snapshot", snapshot)
    result = manager.snapshot_to_dict(asyncio.run(manager.get_snapshot("anonymous", "c1")))
    assert result["blocks"]["facts"] == []
    assert result["blocks"]["episodes"] == []
    assert "secret" not in str(result)


def test_flush_is_pending_without_advancing_source_cursor_and_direct_fact_writes_disabled(client, manager):
    receipt = begin(client).json()
    finish(client, receipt)
    result = client.post("/memory/flush", json={"user_id": "alice", "conv_id": receipt["conv_id"], "request_id": "r1"})
    assert result.status_code == 200
    assert result.json()["status"] == "PENDING"
    assert result.json()["mutated"] is False
    assert result.json()["covered_source_seq"] == 0
    assert client.get("/memory/facts", params={"user_id": "alice"}).json()["facts"] == []
    direct = client.post("/memory/facts", json={"user_id": "alice", "category": "preference", "content": "forged"})
    assert direct.status_code == 501


def test_clear_receipt_is_audited_and_idempotent(client):
    receipt = begin(client).json()
    finish(client, receipt)
    payload = {"user_id": "alice", "conv_id": receipt["conv_id"], "request_id": "clear-one"}
    first = client.post("/memory/clear", json=payload)
    second = client.post("/memory/clear", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["revision"] == second.json()["revision"]


@pytest.mark.parametrize("path,method,body", [
    ("/memory/turn/begin", "post", {"user_id": "alice", "request_id": "r1", "message": "hello"}),
    ("/memory/context", "post", {"user_id": "alice", "conv_id": "c1"}),
    ("/memory/facts?user_id=alice", "get", None),
    ("/memory/clear", "post", {"user_id": "alice", "conv_id": "c1"}),
    ("/memory/settings", "post", {"user_id": "alice", "enabled": False}),
    ("/memory/all?user_id=alice", "delete", None),
])
def test_storage_errors_return_503(client, manager, monkeypatch, path, method, body):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    for name in ("begin_turn", "get_snapshot", "list_facts", "clear_facts", "set_long_term_enabled", "delete_all"):
        monkeypatch.setattr(manager._repo, name, unavailable)
    response = client.request(method, path, json=body)
    assert response.status_code == 503


def test_sqlite_manager_never_imports_redis_or_chroma(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("redis") or name.startswith("chromadb"):
            pytest.fail(f"SQLite memory must not import {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    manager = MemoryManager(db_path=str(tmp_path / "separate.sqlite3"), api_key="")
    asyncio.run(manager.close())


def test_lifespan_runs_and_stops_recovery_worker_without_optional_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "startup.sqlite3"))
    monkeypatch.setenv("MEMORY_WORKER_POLL_SECONDS", "0.1")
    monkeypatch.setattr(main, "initialize_business", lambda *args: None)
    monkeypatch.setattr(main, "_initialize_optional_services", lambda *args: None)
    with TestClient(main.app) as client:
        worker = main.app.state.memory_worker
        task = main.app.state.memory_worker_task
        assert worker is not None and not task.done()
        assert begin(client).status_code == 200
    assert task.done()
    assert not task.cancelled()


def test_sqlite_open_failure_allows_degraded_startup(tmp_path, monkeypatch):
    # An existing directory cannot be opened as the SQLite database file.
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path))
    monkeypatch.setattr(main, "initialize_business", lambda *args: None)
    monkeypatch.setattr(main, "_initialize_optional_services", lambda *args: None)
    with TestClient(main.app) as client:
        assert main.app.state.memory_worker is None
        assert begin(client).status_code == 503


@pytest.mark.parametrize("env", [
    {"MEMORY_CONTEXT_MAX_CHARS": "abc"}, {"MEMORY_CONTEXT_MAX_CHARS": "0"},
    {"MEMORY_LONG_TERM_ENABLED": "perhaps"}, {"MEMORY_SESSION_LEASE_SECONDS": "4"},
    {"MEMORY_WORKER_POLL_SECONDS": "nan"}, {"MEMORY_NAMESPACE": " "}, {"MEMORY_DB_PATH": ""},
])
def test_memory_settings_reject_invalid_environment(env):
    with pytest.raises(ValueError):
        MemorySettings.from_env(Path("/tmp/lumenmind"), env)


def test_replayed_finish_does_not_double_count_metric(client, monkeypatch):
    class Counter:
        count = 0

        def inc(self):
            self.count += 1

    counter = Counter()
    monkeypatch.setattr(main, "MEMORY_TURNS", counter)
    receipt = begin(client).json()
    assert finish(client, receipt).status_code == 200
    assert finish(client, receipt).status_code == 200
    assert counter.count == 1


def test_context_required_data_over_budget_is_413(client, manager, monkeypatch):
    async def oversized(*args):
        return MemorySnapshot(2, "READY", "lumenmind", "alice", "c1", 1, 1, 0, 0,
            facts=[{"fact_id": "f1", "category": "preference", "content": "x" * 4000}])

    monkeypatch.setattr(manager, "get_snapshot", oversized)
    response = client.post("/memory/context", json={"user_id": "alice", "conv_id": "c1"})
    assert response.status_code == 413


def test_settings_are_per_user_and_disabled_turns_are_not_extractable(client, manager):
    assert client.post("/memory/settings", json={"user_id": "alice", "enabled": False}).status_code == 200
    assert manager.long_term_enabled is True
    alice = begin(client).json()
    finish(client, alice)
    bob = client.post("/memory/turn/begin", json={"user_id": "bob", "request_id": "b1", "message": "hello"})
    assert bob.status_code == 200
    with sqlite3.connect(manager._repo.path) as db:
        eligibility = dict(db.execute("SELECT user_id,memory_eligible FROM memory_turn"))
    assert eligibility == {"alice": 0, "bob": 1}
    assert client.post("/memory/flush", json={"user_id": "alice", "conv_id": alice["conv_id"]}).json()["status"] == "DISABLED"


def test_global_memory_ceiling_blocks_enabling_and_future_extraction(tmp_path):
    manager = MemoryManager(db_path=str(tmp_path / "disabled.sqlite3"), long_term_enabled=False)
    try:
        changed = asyncio.run(manager.set_long_term_enabled("alice", True))
        assert changed["status"] == "DISABLED"
        receipt = asyncio.run(manager.begin_turn("alice", None, "r1", "remember this"))
        assert receipt.status == "RECEIVED"
        with sqlite3.connect(manager._repo.path) as db:
            assert db.execute("SELECT memory_eligible FROM memory_turn").fetchone()[0] == 0
        assert asyncio.run(manager.flush("alice"))["status"] == "DISABLED"
    finally:
        asyncio.run(manager.close())
