"""Crash, stale lease, privacy barrier and restart behavior on real SQLite."""
import pytest

from memory.repository import ConflictError, LeaseError, MemoryRepository


@pytest.fixture
def repo(tmp_path):
    return MemoryRepository(str(tmp_path / "memory.sqlite3"))


def expire(repo, conv="c"):
    with repo._connection() as db:
        db.execute("UPDATE memory_session SET lease_until='2000-01-01T00:00:00+00:00' WHERE conv_id=?",(conv,))


def complete(repo, request="r", conv="c", message="hello", response="answer"):
    receipt = repo.begin_turn("u",conv,request,message)
    return repo.finish_turn("u",conv,request,receipt.lease_token,response=response)


def test_new_begin_recovers_expired_input_before_taking_lease(repo):
    old = repo.begin_turn("u","c","old","original input")
    expire(repo)
    new = MemoryRepository(repo.path).begin_turn("u","c","new","new input")
    assert new.lease_token.startswith("2:")
    assert (new.turn_seq,new.source_seq,new.context_revision) == (2,2,1)
    with pytest.raises(LeaseError):
        repo.renew_turn("u","c","old",old.lease_token)
    late = repo.finish_turn("u","c","old",old.lease_token,response="late result")
    assert late.replayed and late.status == "INTERRUPTED" and late.response is None
    snapshot = repo.get_snapshot("u","c",exclude_request_id="new")
    assert [(x["role"],x["content"]) for x in snapshot.recent_messages] == [("user","original input")]
    with repo._connection() as db:
        assert db.execute("SELECT lease_owner FROM memory_session").fetchone()[0] == "new"
        assert db.execute("SELECT COUNT(*) FROM memory_job WHERE kind='fact_extract'").fetchone()[0] == 1


def test_expired_lease_cannot_finish_or_renew_even_before_recovery(repo):
    old = repo.begin_turn("u","c","old","input")
    expire(repo)
    with pytest.raises(LeaseError):
        repo.finish_turn("u","c","old",old.lease_token,response="late")
    with pytest.raises(LeaseError):
        repo.renew_turn("u","c","old",old.lease_token)
    assert repo.recover_interrupted_turns() == 1
    assert repo.recover_interrupted_turns() == 0


def test_failed_and_cancelled_input_has_no_invented_assistant(repo):
    for index,status in enumerate(("FAILED","CANCELLED")):
        receipt = repo.begin_turn("u","c",str(index),f"input {index}")
        repo.finish_turn("u","c",str(index),receipt.lease_token,status,"must not appear")
    assert [x["content"] for x in repo.get_snapshot("u","c").recent_messages] == ["input 0","input 1"]


def test_clear_deduplicates_and_fences_old_context_and_claim(repo):
    complete(repo)
    fact = repo.add_fact("u","preference","short replies",source_seqs=[1],evidence="user evidence")
    job = repo.claim_due_jobs()[0]
    with repo._connection() as db:
        db.execute("UPDATE memory_session SET current_summary='{\"user_goal\":\"short replies\"}',summary_generation=1")
    result = repo.clear_facts("u","clear-1")
    assert repo.clear_facts("u","clear-1") == result
    with pytest.raises(ConflictError):
        repo.retract_fact("u",fact["fact_id"],"clear-1")
    assert not repo.complete_job(job["job_id"],job["claim_token"])
    snapshot = repo.get_snapshot("u","c")
    assert snapshot.erase_epoch == 1 and not snapshot.facts and not snapshot.recent_messages and not snapshot.summary
    complete(repo,"after",message="after deletion")
    assert repo.get_snapshot("u","c").recent_messages[0]["content"] == "after deletion"


def test_retract_preserves_other_fact_but_removes_old_background(repo):
    complete(repo)
    first = repo.add_fact("u","preference","first")
    second = repo.add_fact("u","preference","second")
    repo.retract_fact("u",first["fact_id"],"forget")
    snapshot = repo.get_snapshot("u","c")
    assert [x["fact_id"] for x in snapshot.facts] == [second["fact_id"]]
    assert not snapshot.recent_messages


def test_disable_prevents_disabled_interval_backfill(repo):
    complete(repo)
    repo.set_long_term_enabled("u",False)
    complete(repo,"disabled",message="remember disabled input")
    repo.set_long_term_enabled("u",True)
    complete(repo,"enabled",message="remember enabled input")
    with repo._connection() as db:
        assert [(x[0],x[1]) for x in db.execute("SELECT request_id,memory_eligible FROM memory_turn ORDER BY source_seq")] == [("r",1),("disabled",0),("enabled",1)]
        assert db.execute("SELECT extract_cursor FROM memory_scope").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_job WHERE status='READY'").fetchone()[0] == 1


def test_global_disabled_and_anonymous_never_create_fact_jobs(repo):
    disabled = MemoryRepository(repo.path,long_term_enabled=False)
    for user in ("u","anonymous","demo"):
        active = disabled if user == "u" else repo
        if user == "u":
            active.set_long_term_enabled(user,True)
        receipt = active.begin_turn(user,"c",user,"please remember me")
        active.finish_turn(user,"c",user,receipt.lease_token,response="reply")
    with repo._connection() as db:
        assert db.execute("SELECT SUM(memory_eligible) FROM memory_turn").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_job").fetchone()[0] == 0


def test_delete_conversation_scrubs_originals_and_evidence_and_keeps_tombstone(repo):
    complete(repo,message="private original",response="private assistant")
    fact = repo.add_fact("u","preference","independent fact",source_seqs=[1],evidence="private evidence")
    repo.delete_conversation("u","c")
    with repo._connection() as db:
        row = db.execute("SELECT * FROM memory_turn").fetchone()
        assert row["status"] == "DELETED" and row["user_text"] == "" and row["assistant_text"] is None
        assert not db.execute("SELECT * FROM memory_fact_source").fetchall()
        assert db.execute("SELECT provenance_state FROM memory_fact").fetchone()[0] == "SOURCE_DELETED"
    assert repo.list_facts("u")["facts"][0]["fact_id"] == fact["fact_id"]
    assert repo.begin_turn("u","c","r","private original").status == "DELETED"
    with pytest.raises(ConflictError):
        repo.begin_turn("u","c","new","new request")


def test_delete_all_retains_body_free_barrier_and_old_request_cannot_resurrect(repo):
    complete(repo,message="private body")
    repo.add_fact("u","preference","private fact")
    result = repo.delete_all("u")
    assert result["index_cleanup"] == "PENDING"
    restored = MemoryRepository(repo.path)
    assert restored.begin_turn("u","c","r","private body").status == "DELETED"
    with pytest.raises(ConflictError):
        restored.begin_turn("u","c","new","new body")
    with repo._connection() as db:
        assert db.execute("SELECT erase_epoch,context_floor_source_seq,migration_state FROM memory_scope").fetchone()[:] == (1,1,"ERASED")
        assert db.execute("SELECT COUNT(*) FROM memory_fact").fetchone()[0] == 0
        assert db.execute("SELECT user_text FROM memory_turn").fetchone()[0] == ""
        assert db.execute("SELECT kind,result FROM memory_job").fetchone()[:] == ("index_delete",None)
    new = restored.begin_turn("u",None,"new","new body")
    assert new.source_seq == 2 and new.conv_id != "c"


def test_flush_reports_pending_without_consuming_unjudged_user_input(repo):
    complete(repo,message="translate: please forget my preferences")
    result = repo.flush_user_messages("u")
    assert result["status"] == "PENDING" and result["covered_source_seq"] == 0
    assert not repo.list_facts("u")["facts"]


def test_job_lease_fences_late_completion_and_retry_has_backoff(repo):
    complete(repo)
    job = repo.claim_due_jobs()[0]
    with repo._connection() as db:
        db.execute("UPDATE memory_job SET lease_until='2000-01-01T00:00:00+00:00'")
    assert not repo.complete_job(job["job_id"],job["claim_token"])
    new = repo.claim_due_jobs()[0]
    assert new["claim_token"] != job["claim_token"]
    assert repo.retry_job(new["job_id"],new["claim_token"],"transient") == "RETRY_WAIT"
    assert not repo.claim_due_jobs()
