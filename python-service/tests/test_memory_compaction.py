"""Rolling summaries retain raw turns and fence concurrent/forgotten candidates."""
import asyncio
import json
import sqlite3

import pytest

from memory.model_client import MemoryModelClient
from memory.repository import ConflictError, MemoryRepository
from memory.summary_repository import SummaryRepositoryMixin, validate_summary_result
from memory.summary_service import SummaryService, SummaryServiceSettings


class Repository(MemoryRepository, SummaryRepositoryMixin):
    pass


SUMMARY = {"user_goal": "排查登录问题", "constraints": ["user 要求保留日志"],
    "confirmed_facts": [], "progress": ["assistant 仅建议重试，尚未执行退款"],
    "open_questions": ["缺少错误时间"], "next_step": "请用户补充时间"}


@pytest.fixture
def repo(tmp_path):
    return Repository(str(tmp_path / "memory.sqlite3"))


def turn(repo, seq, status="COMPLETED", user="alice", conv="c1", text=None):
    result = repo.begin_turn(user, conv, f"r{seq}", text or f"user original {seq}")
    if status != "RECEIVED":
        repo.finish_turn(user, conv, result.request_id, result.lease_token, status, f"assistant suggestion {seq}")
    return result


def history(repo, count=13, **kwargs):
    for seq in range(1, count + 1):
        turn(repo, seq, **kwargs)


def schedule(repo, **kwargs):
    assert repo.schedule_due_summary_jobs(**kwargs) == 1
    job = repo.claim_summary_job()
    assert job is not None
    return job


def test_compaction_keeps_three_complete_turns_and_writes_audit_episode_outbox(repo):
    history(repo)
    job = schedule(repo)
    assert [item["turn_seq"] for item in job["turns"]] == list(range(1, 11))
    assert job["from_seq"] == 1 and job["to_seq"] == 10
    result = repo.apply_summary_result(job["job_id"], job["claim_token"], SUMMARY)
    assert result["generation"] == 1 and result["covered_through_turn"] == 10
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 13
        assert db.execute("SELECT user_text FROM memory_turn WHERE turn_seq=1").fetchone()[0] == "user original 1"
        audit = db.execute("SELECT covered_through_turn,base_context_revision,prompt_version FROM memory_summary").fetchone()
        assert audit == (10, 13, "summary-v1")
        assert db.execute("SELECT from_turn,to_turn,source_generation FROM memory_episode").fetchone() == (1, 10, 1)
        outbox = db.execute("SELECT result FROM memory_job WHERE kind='index_upsert'").fetchone()
        assert json.loads(outbox[0])["episode_id"] == result["episode_id"]
    assert repo.apply_summary_result(job["job_id"], job["claim_token"], SUMMARY) == result
    assert repo.schedule_due_summary_jobs() == 0


def test_failed_cancelled_interrupted_turns_count_and_received_is_never_summarized(repo):
    for seq in range(1, 14):
        turn(repo, seq, status=["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"][seq % 4])
    active = turn(repo, 14, status="RECEIVED")
    job = schedule(repo)
    assert job["to_seq"] == 10
    for item in job["turns"]:
        assert item["status"] != "RECEIVED"
        if item["status"] != "COMPLETED":
            assert item["assistant_text"] is None
    assert active.turn_seq == 14


def test_character_threshold_compacts_fewer_turns_without_slicing(repo):
    for seq in range(1, 6):
        turn(repo, seq, text="原文" * 2000)
    job = schedule(repo)
    assert job["to_seq"] == 2
    assert job["turns"][0]["user_text"] == "原文" * 2000


def test_generation_merges_previous_summary_with_only_new_uncovered_complete_turns(repo):
    history(repo)
    one = schedule(repo)
    repo.apply_summary_result(one["job_id"], one["claim_token"], SUMMARY)
    for seq in range(14, 24):
        turn(repo, seq)
    two = schedule(repo)
    assert two["previous_summary"] == SUMMARY
    assert two["from_seq"] == 11 and two["to_seq"] == 20
    result = repo.apply_summary_result(two["job_id"], two["claim_token"], {**SUMMARY, "next_step": "新的下一步"})
    assert result["generation"] == 2
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 2
        assert db.execute("SELECT from_turn,to_turn FROM memory_episode ORDER BY source_generation DESC LIMIT 1").fetchone() == (1, 20)


@pytest.mark.parametrize("payload", [
    {}, {**SUMMARY, "extra": "bad"}, {**SUMMARY, "progress": [{"success": True}]},
    {key: "" for key in SUMMARY}, {**SUMMARY, "user_goal": "x" * 4001},
    {**SUMMARY, "user_goal": "\ud800"},
])
def test_invalid_summary_keeps_previous_generation_and_raw_sources(repo, payload):
    history(repo)
    job = schedule(repo)
    with pytest.raises(ValueError):
        repo.apply_summary_result(job["job_id"], job["claim_token"], payload)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT summary_generation,current_summary FROM memory_session").fetchone() == (0, "")
        assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 13


def test_concurrent_completed_turn_invalidates_candidate_without_consuming_failure_budget(repo):
    history(repo)
    job = schedule(repo)
    turn(repo, 14)
    with pytest.raises(ConflictError):
        repo.apply_summary_result(job["job_id"], job["claim_token"], SUMMARY)
    failed = repo.fail_summary_job(job["job_id"], job["claim_token"], "ConflictError", conflict=True)
    assert failed == {"status": "CANCELLED", "failure_count": 0}
    replacement = schedule(repo)
    assert replacement["expected_revision"] == 14
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 0


def test_clear_and_delete_barriers_reject_inflight_summary(repo):
    history(repo)
    job = schedule(repo)
    repo.clear_facts("alice", request_id="clear1")
    with pytest.raises(ConflictError):
        repo.apply_summary_result(job["job_id"], job["claim_token"], SUMMARY)
    assert repo.schedule_due_summary_jobs() == 0
    for seq in range(14, 27):
        turn(repo, seq)
    replacement = schedule(repo)
    assert replacement["previous_summary"] == {}
    assert replacement["from_seq"] == 14
    repo.delete_conversation("alice", "c1")
    with pytest.raises(ConflictError):
        repo.apply_summary_result(replacement["job_id"], replacement["claim_token"], SUMMARY)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 0


def test_generation_after_forgetting_does_not_collide_with_audit_history(repo):
    history(repo)
    job = schedule(repo)
    repo.apply_summary_result(job["job_id"], job["claim_token"], SUMMARY)
    repo.clear_facts("alice", request_id="clear1")
    for seq in range(14, 27):
        turn(repo, seq)
    two = schedule(repo)
    result = repo.apply_summary_result(two["job_id"], two["claim_token"], SUMMARY)
    assert result["generation"] == 2
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 2


def test_expired_claim_is_reclaimed_and_old_token_cannot_commit(repo):
    history(repo)
    one = schedule(repo)
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE memory_job SET lease_until='2000-01-01T00:00:00Z' WHERE job_id=?", (one["job_id"],))
    two = repo.claim_summary_job()
    assert two["job_id"] == one["job_id"] and two["claim_token"] != one["claim_token"]
    with pytest.raises(ConflictError):
        repo.apply_summary_result(one["job_id"], one["claim_token"], SUMMARY)
    assert repo.apply_summary_result(two["job_id"], two["claim_token"], SUMMARY)["status"] == "WRITTEN"


def test_disabled_longterm_still_compacts_without_creating_episode_or_index_job(repo):
    repo.set_long_term_enabled("alice", False)
    history(repo)
    job = schedule(repo)
    result = repo.apply_summary_result(job["job_id"], job["claim_token"], SUMMARY)
    assert result["episode_id"] is None
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_episode").fetchone()[0] == 0


class FakeSummaryModel:
    def __init__(self, value=SUMMARY, delay=0, error=None):
        self.value = value; self.delay = delay; self.error = error; self.prompts = []

    async def generate_json(self, system, prompt):
        self.prompts.append((system, json.loads(prompt)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.value

    async def close(self):
        pass


def test_service_tick_does_not_block_recovery_during_model_and_commits_on_close(repo):
    async def scenario():
        history(repo)
        model = FakeSummaryModel(delay=0.02)
        service = SummaryService(repo, model)
        await service.tick()
        assert len(service._tasks) == 1
        await service.close()
        assert model.prompts[0][1]["complete_turns"][0]["turn_seq"] == 1
        assert "不是业务工具执行证据" in model.prompts[0][0]
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 1
    asyncio.run(scenario())


def test_model_error_retries_three_times_then_quarantines_without_raw_loss(repo):
    async def scenario():
        history(repo)
        service = SummaryService(repo, FakeSummaryModel(error=RuntimeError("PRIVATE_CONTENT")))
        for attempt in range(3):
            await service.tick()
            await asyncio.gather(*list(service._tasks.values()))
            with sqlite3.connect(repo.path) as db:
                status, failures, code = db.execute("SELECT status,failure_count,error_code FROM memory_job WHERE kind='summary'").fetchone()
                assert status == ("QUARANTINED" if attempt == 2 else "RETRY_WAIT")
                assert failures == attempt + 1 and code == "RuntimeError"
                db.execute("UPDATE memory_job SET next_run_at='2000-01-01T00:00:00Z' WHERE kind='summary'")
        await service.close()
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 13
            assert db.execute("SELECT COUNT(*) FROM memory_summary").fetchone()[0] == 0
    asyncio.run(scenario())


def test_unconfigured_summary_model_records_retry_without_advancing_coverage(repo):
    async def scenario():
        history(repo)
        service = SummaryService(repo, MemoryModelClient(model="configured", api_key=""))
        await service.tick()
        await service.close()
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT status,error_code FROM memory_job WHERE kind='summary'").fetchone() == ("RETRY_WAIT", "MemoryModelUnavailable")
            assert db.execute("SELECT summary_generation FROM memory_session").fetchone()[0] == 0
    asyncio.run(scenario())


def test_later_summary_failure_preserves_old_generation_content_and_coverage(repo):
    history(repo)
    one = schedule(repo)
    repo.apply_summary_result(one["job_id"], one["claim_token"], SUMMARY)
    for seq in range(14, 24):
        turn(repo, seq)
    two = schedule(repo)
    with pytest.raises(ValueError):
        repo.apply_summary_result(two["job_id"], two["claim_token"], {"user_goal": "invalid"})
    repo.fail_summary_job(two["job_id"], two["claim_token"], "ValueError")
    with sqlite3.connect(repo.path) as db:
        generation, content = db.execute("SELECT summary_generation,current_summary FROM memory_session").fetchone()
        assert generation == 1 and json.loads(content) == SUMMARY
        assert db.execute("SELECT MAX(covered_through_turn) FROM memory_summary").fetchone()[0] == 10
        assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 23


def test_stale_scheduled_job_cancels_before_model_and_replacement_uses_new_revision(repo):
    history(repo)
    assert repo.schedule_due_summary_jobs() == 1
    turn(repo, 14)
    assert repo.claim_summary_job() is None
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT status,failure_count FROM memory_job WHERE kind='summary'").fetchone() == ("CANCELLED", 0)
    assert repo.schedule_due_summary_jobs() == 1
    assert repo.claim_summary_job()["expected_revision"] == 14


def test_empty_array_and_nested_content_never_count_as_valid_summary():
    with pytest.raises(ValueError):
        validate_summary_result({key: [] for key in SUMMARY})
