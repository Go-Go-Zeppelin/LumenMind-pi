"""Real SQLite contract tests; no Redis, model, or vector service required."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from memory.repository import ConflictError, LeaseError, MemoryRepository


@pytest.fixture
def repo(tmp_path):
    return MemoryRepository(str(tmp_path / "memory.sqlite3"))


def finish(repo, request="r", conv="c", message="hello", response="answer", **kwargs):
    receipt = repo.begin_turn("u", conv, request, message, **kwargs)
    return repo.finish_turn("u", receipt.conv_id, request, receipt.lease_token, response=response)


def test_schema_is_versioned_and_upgrades_preview_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    migration = Path(__file__).parents[1] / "memory/migrations/001_memory.sql"
    with sqlite3.connect(path) as db:
        db.executescript(migration.read_text())
        db.executescript("CREATE TABLE memory_schema_version(version INTEGER); INSERT INTO memory_schema_version VALUES(1);")
        db.execute("INSERT INTO memory_scope(namespace,user_id,created_at,updated_at) VALUES('lumenmind','u','old','old')")
    repo = MemoryRepository(str(path))
    MemoryRepository(str(path))  # Restart does not replay ALTER TABLE.
    with repo._connection() as db:
        assert db.execute("SELECT version FROM memory_schema_version").fetchone()[0] == 2
        assert db.execute("SELECT created_at FROM memory_scope").fetchone()[0] == "old"
    assert finish(repo).status == "COMPLETED"


def test_auto_conversation_idempotency_survives_restart(repo):
    first = repo.begin_turn("u", None, "r1", "hello", "client-key")
    retry = MemoryRepository(repo.path).begin_turn("u", None, "r2", "hello", "client-key")
    assert retry.replayed
    assert (retry.request_id, retry.conv_id, retry.source_seq, retry.lease_token) == (first.request_id,first.conv_id,1,first.lease_token)
    for conv, message in ((None,"changed"),("different","hello")):
        with pytest.raises(ConflictError):
            repo.begin_turn("u",conv,"r3",message,"client-key")


def test_request_key_and_client_key_cannot_map_to_different_turns(repo):
    finish(repo,"a","a",idempotency_key="key-a")
    finish(repo,"b","b",idempotency_key="key-b")
    with pytest.raises(ConflictError):
        repo.begin_turn("u","a","a","hello","key-b")


def test_terminal_retries_keep_original_reply_revision_and_job(repo):
    first = finish(repo)
    finish(repo,"r2",response="second")
    retry = repo.finish_turn("u","c","r","incorrect-old-token","FAILED","replacement")
    assert retry.replayed
    assert retry.response == "answer"
    assert retry.status == "COMPLETED"
    assert retry.context_revision == first.context_revision == 1
    begin_retry = repo.begin_turn("u","c","r","hello")
    assert begin_retry.response == "answer" and begin_retry.replayed
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_job").fetchone()[0] == 2
        assert db.execute("SELECT next_turn_seq FROM memory_session").fetchone()[0] == 3


def test_independent_connections_allocate_one_live_session_lease(repo):
    repos = [MemoryRepository(repo.path) for _ in range(8)]
    def attempt(pair):
        index, other = pair
        try:
            return other.begin_turn("u","c",f"r{index}","hello")
        except LeaseError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt,enumerate(repos)))
    assert len([x for x in outcomes if x]) == 1
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 1
        assert db.execute("SELECT next_source_seq FROM memory_scope").fetchone()[0] == 2


def test_concurrent_same_idempotency_key_is_one_durable_request(repo):
    repos = [MemoryRepository(repo.path) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda item: item[1].begin_turn("u",None,f"r{item[0]}","hello","shared"),enumerate(repos)))
    assert len({x.request_id for x in results}) == 1
    assert len({x.conv_id for x in results}) == 1
    assert sum(not x.replayed for x in results) == 1


def test_renew_checks_owner_request_and_conversation(repo):
    first = repo.begin_turn("u","a","first","one")
    second = repo.begin_turn("u","b","second","two")
    for conv, request, token in (("a","second",first.lease_token),("b","first",second.lease_token),("a","first",second.lease_token)):
        with pytest.raises(LeaseError):
            repo.renew_turn("u",conv,request,token)
    assert repo.renew_turn("u","a","first",first.lease_token)


def test_snapshot_is_one_sqlite_read_transaction(repo):
    finish(repo)
    repo.add_fact("u","preference","short replies")
    writer = MemoryRepository(repo.path)
    original = repo._connection
    fired = False
    @contextmanager
    def interleaved_read():
        nonlocal fired
        with original() as db:
            def callback(statement):
                nonlocal fired
                if not fired and statement.startswith("SELECT * FROM memory_session"):
                    fired = True
                    writer.clear_facts("u","delete-during-read")
            db.set_trace_callback(callback)
            yield db
    repo._connection = interleaved_read
    snapshot = repo.get_snapshot("u","c")
    assert fired
    assert snapshot.erase_epoch == 0
    assert snapshot.facts[0]["content"] == "short replies"
    assert snapshot.recent_messages[0]["content"] == "hello"
    latest = repo.get_snapshot("u","c")
    assert latest.erase_epoch == 1 and not latest.facts and not latest.recent_messages


def test_namespace_and_user_sequence_are_isolated(repo):
    a = repo.begin_turn("u","c","a","a")
    b = repo.begin_turn("u","d","b","b")
    c = repo.begin_turn("v","c","a","c")
    other = MemoryRepository(repo.path,"other").begin_turn("u","c","a","d")
    assert (a.source_seq,b.source_seq,c.source_seq,other.source_seq) == (1,2,1,1)
