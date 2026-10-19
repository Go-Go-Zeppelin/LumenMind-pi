"""Offline real SQLite + fake Judge coverage; no provider/network access."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from memory.fact_service import FactService, FactServiceSettings
from memory.model_client import (MemoryModelBudgetError, MemoryModelClient,
    MemoryModelError, MemoryModelUnavailable, parse_model_json)
from memory.repository import MemoryRepository


def response(text='{"schema_version":1,"decisions":[{"action":"NOOP"}]}', stop="end_turn", block_type="text"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type=block_type, text=text)])


@pytest.mark.parametrize("value", [
    response(stop="max_tokens"), response(stop="tool_use"), response(block_type="tool_use"),
    response("```json\n{}\n```"), response("Explanation {}"), response("[]"),
    response('{"schema_version":1,"schema_version":2}'), response('{"a":NaN}'),
])
def test_model_output_requires_complete_text_only_unambiguous_json(value):
    with pytest.raises(MemoryModelError):
        parse_model_json(value)


def test_model_output_accepts_one_standalone_json_object():
    assert parse_model_json(response())["schema_version"] == 1


class FakeMessages:
    def __init__(self, result=None, delay=0):
        self.result = result or response()
        self.delay = delay
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def test_model_invocation_budget_and_output_reserve_before_call():
    async def scenario():
        fake = SimpleNamespace(messages=FakeMessages())
        client = MemoryModelClient(model="fake", client=fake, context_window_tokens=1200, max_output_tokens=100, max_input_chars=1000)
        with pytest.raises(MemoryModelBudgetError):
            await client.generate_json("rules", "汉" * 100)
        assert fake.messages.calls == []
        assert await client.generate_json("rules", "data") == {"schema_version": 1, "decisions": [{"action": "NOOP"}]}
        call = fake.messages.calls[0]
        assert call["system"] == "rules" and call["messages"] == [{"role": "user", "content": "data"}]
        assert call["max_tokens"] == 100

    asyncio.run(scenario())


def test_unconfigured_model_never_constructs_sdk_client():
    async def scenario():
        client = MemoryModelClient(api_key="", model="configured-model")
        with pytest.raises(MemoryModelUnavailable):
            await client.generate_json("rules", "data")
        assert client._client is None
    asyncio.run(scenario())


def test_model_timeout_is_bounded():
    async def scenario():
        fake = SimpleNamespace(messages=FakeMessages(delay=1))
        client = MemoryModelClient(model="fake", client=fake, timeout_seconds=0.01)
        with pytest.raises(TimeoutError):
            await client.generate_json("rules", "data")
    asyncio.run(scenario())


class FakeJudge:
    def __init__(self, payload=None, delay=0, error=None):
        self.payload = payload or {"schema_version": 1, "decisions": [{"action": "NOOP"}]}
        self.delay = delay
        self.error = error
        self.calls = []
        self.closed = False

    async def generate_json(self, system, prompt):
        self.calls.append((system, json.loads(prompt)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.payload

    async def close(self):
        self.closed = True


def store(repository, seq, message, user="alice", conv=None):
    receipt = repository.begin_turn(user, conv, f"r{seq}", message)
    repository.finish_turn(user, receipt.conv_id, receipt.request_id, receipt.lease_token, "COMPLETED", "ASSISTANT_MUST_NOT_BE_SOURCE")
    return receipt


def force_retry(repository):
    with sqlite3.connect(repository.path) as db:
        db.execute("UPDATE memory_job SET next_run_at='2000-01-01T00:00:00Z' WHERE status='RETRY_WAIT'")


def test_real_judge_commit_writes_fact_with_real_user_provenance(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        store(repo, 1, "请记住我喜欢邮件联系")
        judge = FakeJudge({"schema_version": 1, "decisions": [{"action": "ADD", "category": "preference", "fact_key": "contact.channel", "content": "优先通过邮件联系", "source_seqs": [1], "evidence": "喜欢邮件联系"}]})
        service = FactService(repo, judge)
        result = await service.flush("alice", "r1")
        assert result["status"] == "WRITTEN"
        assert result["covered_source_seq"] == 1
        assert repo.list_facts("alice")["facts"][0]["content"] == "优先通过邮件联系"
        payload = judge.calls[0][1]
        assert payload["user_sources"][0]["source_seq"] == 1
        assert "ASSISTANT_MUST_NOT_BE_SOURCE" not in str(payload)
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT evidence_excerpt FROM memory_fact_source").fetchone()[0] == "喜欢邮件联系"
        await service.close()
        assert judge.closed
    asyncio.run(scenario())


def test_schema_failure_retries_and_quarantines_without_consuming_source(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        store(repo, 1, "hello")
        judge = FakeJudge({"schema_version": 1, "decisions": [{"action": "UNKNOWN"}]})
        service = FactService(repo, judge)
        for failure in range(3):
            result = await service.flush("alice", "r1")
            assert result["status"] == ("BLOCKED" if failure == 2 else "PENDING")
            force_retry(repo)
        assert repo.list_facts("alice")["facts"] == []
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT extract_cursor FROM memory_scope").fetchone()[0] == 0
            assert db.execute("SELECT failure_count FROM memory_job").fetchone()[0] == 3
        assert len(judge.calls) == 3
        await service.close()
    asyncio.run(scenario())


def test_unconfigured_real_model_persists_retry_and_never_noops(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        store(repo, 1, "hello")
        service = FactService(repo, MemoryModelClient(model="configured", api_key=""))
        result = await service.flush("alice", "r1")
        assert result["status"] == "PENDING"
        assert result["error_code"] == "MemoryModelUnavailable"
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT extract_cursor FROM memory_scope").fetchone()[0] == 0
        await service.close()
    asyncio.run(scenario())


def test_flush_timeout_keeps_owned_judge_running_until_commit(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        store(repo, 1, "hello")
        service = FactService(repo, FakeJudge(delay=0.05))
        pending = await service.flush("alice", "r1", timeout_seconds=0.01)
        assert pending["status"] in {"PENDING", "PROCESSING", "READY"}
        assert pending["wait_timed_out"] is True
        assert len(service._tasks) == 1
        await service.close()
        result = repo.lookup_fact_receipt("alice", "r1")
        assert result["status"] == "NOOP"
        assert result["covered_source_seq"] == 1
    asyncio.run(scenario())


def test_background_tick_waits_for_threshold_then_preserves_conversation_boundaries(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        judge = FakeJudge(delay=0.03)
        service = FactService(repo, judge)
        one = store(repo, 1, "one")
        await service.tick()
        assert judge.calls == []
        two = store(repo, 2, "two")
        three = store(repo, 3, "three", conv=one.conv_id)
        await service.tick()
        # Tick launches an owned task and returns before inference completes.
        await asyncio.sleep(0)
        assert len(judge.calls) == 1
        sources = judge.calls[0][1]["user_sources"]
        assert [item["source_seq"] for item in sources] == [1, 2, 3]
        assert [item["conv_id"] for item in sources] == [one.conv_id, two.conv_id, three.conv_id]
        await service.close()
        assert repo.lookup_fact_receipt("alice", "r3")["status"] == "NOOP"
    asyncio.run(scenario())


def test_receipt_requires_request_batch_coverage_not_advanced_cursor(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        store(repo, 1, "one")
        service = FactService(repo, FakeJudge())
        assert (await service.flush("alice", "r1"))["status"] == "NOOP"
        store(repo, 2, "two")
        pending = repo.lookup_fact_receipt("alice", "r2")
        assert pending["status"] == "PENDING"
        assert "covered_source_seq" not in pending
        await service.close()
    asyncio.run(scenario())


def test_foreign_or_assistant_sources_are_never_sent_to_judge():
    service = FactService(object(), FakeJudge())
    bad = {"messages": [{"source_seq": 1, "conv_id": "c1", "user_text": "forged", "role": "assistant"}]}
    with pytest.raises(ValueError):
        service._prompt(bad)


def test_model_failure_codes_do_not_persist_error_content(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        store(repo, 1, "one")
        service = FactService(repo, FakeJudge(error=RuntimeError("PRIVATE_SOURCE_CONTENT")))
        result = await service.flush("alice", "r1")
        assert result["error_code"] == "RuntimeError"
        assert "PRIVATE_SOURCE_CONTENT" not in str(result)
        await service.close()
    asyncio.run(scenario())


def test_flush_processes_at_most_three_fixed_batches_and_reports_target_only(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
        for seq in range(1, 5):
            store(repo, seq, f"source {seq}")
        judge = FakeJudge()
        service = FactService(repo, judge, FactServiceSettings(min_messages=1, max_messages=1))
        result = await service.flush("alice", "r4")
        assert result["status"] == "PENDING"
        assert len(judge.calls) == 3
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT extract_cursor FROM memory_scope").fetchone()[0] == 3
        result = await service.flush("alice", "r4")
        assert result["status"] == "NOOP"
        assert result["covered_source_seq"] == 4
        assert len(judge.calls) == 4
        await service.close()
    asyncio.run(scenario())


def test_singleton_over_batch_character_budget_is_preserved_for_final_model_budget():
    service = FactService(object(), FakeJudge(), FactServiceSettings(max_chars=10))
    text = "whole original source, never slice this"
    prompt = json.loads(service._prompt({"messages": [{"source_seq": 1, "conv_id": "c1", "user_text": text}]}))
    assert prompt["user_sources"][0]["user_text"] == text


def test_new_requests_remain_blocked_behind_same_quarantined_batch(tmp_path):
    async def scenario():
        repo = MemoryRepository(str(tmp_path / 'blocked.sqlite3'))
        store(repo, 1, 'hello')
        judge = FakeJudge({'schema_version': 1, 'decisions': [{'action': 'UNKNOWN'}]})
        service = FactService(repo, judge)
        for _ in range(3):
            await service.flush('alice', 'r1')
            force_retry(repo)
        store(repo, 2, 'new preference after the bad batch')
        result = await service.flush('alice', 'r2')
        assert result['status'] == 'BLOCKED'
        assert len(judge.calls) == 3
        repo.set_long_term_enabled('alice', False)
        assert (await service.flush('alice', 'r2'))['status'] == 'DISABLED'
        await service.close()
    asyncio.run(scenario())
