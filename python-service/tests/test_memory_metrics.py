"""Operational metrics use bounded labels, isolated registries and aggregate SQL."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from memory.metrics import MemoryMetrics, repository_status
from memory.repository import MemoryRepository


@pytest.fixture
def metrics():
    registry = CollectorRegistry()
    return MemoryMetrics(registry), registry


def sample(registry, name, labels=None):
    return registry.get_sample_value("lumenmind_memory_" + name, labels or {})


def test_model_duration_usage_and_cache_buckets(metrics):
    observer, registry = metrics
    observer.observe_model("fact_extract", "WRITTEN", 1.5, {
        "input_tokens": 11, "output_tokens": 7,
        "cache_read_input_tokens": 101, "cache_creation_input_tokens": 31,
    })
    labels = {"kind": "fact", "outcome": "success"}
    assert sample(registry, "model_calls_total", labels) == 1
    assert sample(registry, "model_duration_seconds_sum", labels) == 1.5
    for token_type, value in {"input": 11, "output": 7, "cache_read": 101, "cache_write": 31}.items():
        assert sample(registry, "model_tokens_total", {**labels, "token_type": token_type}) == value


def test_accepts_sdk_and_pi_usage_without_double_counting(metrics):
    observer, registry = metrics
    observer.observe_model("summary", "success", .25, SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=10))
    observer.observe_model("summary", "success", .25, {"input": 6, "output": 3, "cacheRead": 20, "cacheWrite": 40})
    labels = {"kind": "summary", "outcome": "success"}
    assert sample(registry, "model_tokens_total", {**labels, "token_type": "input"}) == 11
    assert sample(registry, "model_tokens_total", {**labels, "token_type": "cache_read"}) == 30
    assert sample(registry, "model_tokens_total", {**labels, "token_type": "cache_write"}) == 40


def test_malformed_usage_cannot_create_nan_or_negative_metrics(metrics):
    observer, registry = metrics
    observer.observe_model("fact", "FAILED", -9, {"input_tokens": float("nan"), "output_tokens": -1, "cache_read_input_tokens": "bad", "cache_creation_input_tokens": True})
    observer.observe_model("fact", "FAILED", float("inf"), None)
    labels = {"kind": "fact", "outcome": "failure"}
    assert sample(registry, "model_calls_total", labels) == 2
    assert sample(registry, "model_duration_seconds_sum", labels) == 0
    for token_type in ("input", "output", "cache_read", "cache_write"):
        assert sample(registry, "model_tokens_total", {**labels, "token_type": token_type}) == 0


def test_context_summary_capacity_and_job_observations(metrics):
    observer, registry = metrics
    observer.observe_context(123, False)
    observer.observe_context(80, True)
    observer.observe_summary(18_001, 3_500, 3.25, "WRITTEN")
    observer.observe_job("index_delete", "RETRY_WAIT")
    observer.observe_capacity("facts_chars")
    assert sample(registry, "context_renders_total") == 2
    assert sample(registry, "context_rendered_chars_sum") == 203
    assert sample(registry, "context_truncations_total") == 1
    assert sample(registry, "summaries_total", {"outcome": "success"}) == 1
    assert sample(registry, "summary_chars_sum", {"stage": "before"}) == 18_001
    assert sample(registry, "summary_chars_sum", {"stage": "after"}) == 3_500
    assert sample(registry, "summary_duration_seconds_sum", {"outcome": "success"}) == 3.25
    assert sample(registry, "jobs_total", {"kind": "index", "outcome": "retry"}) == 1
    assert sample(registry, "capacity_rejections_total", {"reason": "fact_block_chars"}) == 1


def test_labels_are_bounded_and_never_contain_arbitrary_text(metrics):
    observer, registry = metrics
    for index in range(50):
        private = f"SECRET_USER_{index}"
        observer.observe_model(private, private, 0)
        observer.observe_job(private, private)
        observer.observe_capacity(private)
    exported = generate_latest(registry).decode()
    assert "SECRET_USER" not in exported
    assert sample(registry, "model_calls_total", {"kind": "other", "outcome": "other"}) == 50
    assert sample(registry, "jobs_total", {"kind": "other", "outcome": "other"}) == 50
    assert sample(registry, "capacity_rejections_total", {"reason": "other"}) == 50


def test_concurrent_updates_are_not_lost(metrics):
    observer, registry = metrics
    def observe(_):
        observer.observe_model("fact", "success", .25, {"input_tokens": 1})
        observer.observe_job("fact", "success")
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(observe, range(1000)))
    labels = {"kind": "fact", "outcome": "success"}
    assert sample(registry, "model_calls_total", labels) == 1000
    assert sample(registry, "model_duration_seconds_sum", labels) == 250
    assert sample(registry, "model_tokens_total", {**labels, "token_type": "input"}) == 1000
    assert sample(registry, "jobs_total", labels) == 1000


def test_registries_are_independent_and_do_not_duplicate_default_collectors():
    first, second = CollectorRegistry(), CollectorRegistry()
    a, b = MemoryMetrics(first), MemoryMetrics(second)
    a.observe_context(10, False)
    b.observe_context(20, False)
    assert sample(first, "context_rendered_chars_sum") == 10
    assert sample(second, "context_rendered_chars_sum") == 20


def add_job(repo, kind, status, job_id):
    with repo._connection() as db:
        db.execute("""INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,status,next_run_at,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (repo.namespace, "PRIVATE_USER", job_id, kind, job_id, status, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"))


def test_repository_status_is_aggregate_namespace_scoped_and_updates_gauges(tmp_path, metrics):
    observer, registry = metrics
    repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
    receipt = repo.begin_turn("PRIVATE_USER", "PRIVATE_CONV", "PRIVATE_REQUEST", "PRIVATE_SOURCE_TEXT")
    repo.add_fact("PRIVATE_USER", "preference", "PRIVATE_FACT_TEXT", source_seqs=[receipt.source_seq], evidence="PRIVATE_SOURCE_TEXT")
    other = MemoryRepository(repo.path, "other")
    other.begin_turn("OTHER_PRIVATE_USER", "c", "r", "OTHER_SOURCE")
    other.add_fact("OTHER_PRIVATE_USER", "preference", "OTHER_FACT")
    add_job(repo, "fact_extract", "READY", "fact")
    add_job(repo, "index_delete", "QUARANTINED", "delete")
    add_job(repo, "PRIVATE_UNKNOWN_KIND", "PROCESSING", "unknown")
    add_job(other, "index_delete", "READY", "other-delete")
    status = observer.refresh_repository(repo)
    assert status["active_facts"] == 1
    assert status["active_fact_content_chars"] == len("PRIVATE_FACT_TEXT")
    assert status["pending_sources"] == 1
    assert status["jobs"]["fact"]["ready"] == 1
    assert status["jobs"]["index"]["quarantined"] == 1
    assert status["jobs"]["other"]["processing"] == 1
    assert status["oldest_pending_seconds"]["fact"] > 60
    assert status["quarantined_jobs"] == 1
    assert status["pending_index_deletes"] == 1
    assert status["recoverable_turns"] == 0
    assert "PRIVATE" not in str(status)
    assert "PRIVATE" not in generate_latest(registry).decode()
    assert sample(registry, "active_facts") == 1
    assert sample(registry, "job_queue", {"kind": "index", "state": "quarantined"}) == 1
    with repo._connection() as db:
        db.execute("UPDATE memory_job SET status='WRITTEN' WHERE namespace=?", (repo.namespace,))
        db.execute("UPDATE memory_session SET lease_until='2000-01-01T00:00:00+00:00' WHERE namespace=?", (repo.namespace,))
    status = observer.refresh_repository(repo)
    assert status["recoverable_turns"] == 1
    assert status["pending_index_deletes"] == 0
    assert sample(registry, "job_queue", {"kind": "index", "state": "quarantined"}) == 0
    assert sample(registry, "oldest_pending_job_seconds", {"kind": "fact"}) == 0


def test_repository_status_excludes_disabled_and_deleted_sources(tmp_path):
    repo = MemoryRepository(str(tmp_path / "memory.sqlite3"))
    repo.begin_turn("u", "c", "r", "stored")
    assert repository_status(repo)["pending_sources"] == 1
    repo.set_long_term_enabled("u", False)
    assert repository_status(repo)["pending_sources"] == 0
    disabled = MemoryRepository(repo.path, long_term_enabled=False)
    assert repository_status(disabled)["pending_sources"] == 0


def test_empty_repository_status_contains_only_zero_numeric_aggregates(tmp_path):
    repo = MemoryRepository(str(tmp_path / "empty.sqlite3"))
    status = repository_status(repo)
    assert status["active_facts"] == status["pending_sources"] == status["pending_index_deletes"] == status["recoverable_turns"] == 0
    assert all(value == 0 for states in status["jobs"].values() for value in states.values())
    assert all(value == 0 for value in status["oldest_pending_seconds"].values())
