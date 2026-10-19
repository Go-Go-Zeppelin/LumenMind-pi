"""Bounded operational memory metrics; never label or expose user/source text.

The default instance shares FastAPI's Prometheus registry. Tests and alternate
apps can inject a CollectorRegistry without registering duplicate collectors.
All database status queries are namespace-wide aggregates in one read snapshot.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

KINDS = ("fact", "summary", "index", "other")
STATES = ("ready", "processing", "retry_wait", "quarantined")
OUTCOMES = frozenset(("success", "failure", "timeout", "conflict", "retry", "quarantined", "noop", "disabled", "pending", "cancelled", "recovered", "other"))
REASONS = frozenset(("count", "fact_chars", "fact_block_chars", "context_chars", "summary_chars", "invalid_output", "storage", "other"))
_KIND_ALIASES = {"fact_extract": "fact", "judge": "fact", "compact": "summary", "compaction": "summary", "index_upsert": "index", "index_delete": "index"}
_OUTCOME_ALIASES = {"written": "success", "completed": "success", "ready": "pending", "failed": "failure", "error": "failure", "retry_wait": "retry", "capacity_exceeded": "failure", "busy": "pending", "incomplete": "pending", "blocked": "quarantined"}
_REASON_ALIASES = {"fact_count": "count", "facts_chars": "fact_block_chars", "total_chars": "context_chars", "capacity_exceeded": "other"}


def _kind(value: Any) -> str:
    value = str(value).lower()
    value = _KIND_ALIASES.get(value, value)
    return value if value in KINDS else "other"


def _outcome(value: Any) -> str:
    value = str(value).lower()
    value = _OUTCOME_ALIASES.get(value, value)
    return value if value in OUTCOMES else "other"


def _number(value: Any) -> float:
    # Metrics must not turn malformed/absent provider usage into pipeline errors.
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _usage(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        return result if isinstance(result, Mapping) else {}
    return {field: getattr(value, field) for field in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
        "input", "output", "cacheRead", "cacheWrite",
    ) if hasattr(value, field)}


class MemoryMetrics:
    def __init__(self, registry: CollectorRegistry = REGISTRY):
        self.model_calls = Counter("lumenmind_memory_model_calls_total", "Memory model calls, separate from primary inference", ("kind", "outcome"), registry=registry)
        self.model_seconds = Histogram("lumenmind_memory_model_duration_seconds", "Memory model latency", ("kind", "outcome"), buckets=(.1, .5, 1, 2, 5, 10, 30, 60, 180), registry=registry)
        self.model_tokens = Counter("lumenmind_memory_model_tokens_total", "Provider-reported memory model token usage", ("kind", "outcome", "token_type"), registry=registry)
        self.jobs = Counter("lumenmind_memory_jobs_total", "Observed durable memory job transitions", ("kind", "outcome"), registry=registry)
        self.context_renders = Counter("lumenmind_memory_context_renders_total", "Memory context render count", registry=registry)
        self.context_chars = Histogram("lumenmind_memory_context_rendered_chars", "Rendered Unicode code points, including wrappers", buckets=(0, 500, 2000, 4000, 8000, 12000, 18000, 24000), registry=registry)
        self.context_truncations = Counter("lumenmind_memory_context_truncations_total", "Context renders that dropped optional records or whole turns", registry=registry)
        self.summaries = Counter("lumenmind_memory_summaries_total", "Summary generation outcomes", ("outcome",), registry=registry)
        self.summary_chars = Histogram("lumenmind_memory_summary_chars", "Summary source and result Unicode code points", ("stage",), buckets=(0, 500, 2000, 4000, 8000, 12000, 18000, 24000, 48000), registry=registry)
        self.summary_seconds = Histogram("lumenmind_memory_summary_duration_seconds", "Summary pipeline duration", ("outcome",), buckets=(.1, .5, 1, 2, 5, 10, 30, 60, 180), registry=registry)
        self.capacity = Counter("lumenmind_memory_capacity_rejections_total", "Atomic capacity rejections", ("reason",), registry=registry)
        self.active_facts = Gauge("lumenmind_memory_active_facts", "Active facts in the configured namespace", registry=registry)
        self.active_fact_chars = Gauge("lumenmind_memory_active_fact_content_chars", "Stored active fact body Unicode code points; excludes JSON wrappers", registry=registry)
        self.job_queue = Gauge("lumenmind_memory_job_queue", "Durable nonterminal jobs in the configured namespace", ("kind", "state"), registry=registry)
        self.oldest_job_seconds = Gauge("lumenmind_memory_oldest_pending_job_seconds", "Age of oldest nonterminal job in the configured namespace", ("kind",), registry=registry)
        self.pending_sources = Gauge("lumenmind_memory_pending_sources", "Eligible unconsumed source messages in the configured namespace", registry=registry)
        self.oldest_source_seconds = Gauge("lumenmind_memory_oldest_pending_source_seconds", "Age of oldest eligible unconsumed source message", registry=registry)
        self.pending_index_deletes = Gauge("lumenmind_memory_pending_index_deletes", "Index cleanup jobs not completed", registry=registry)
        self.recoverable_turns = Gauge("lumenmind_memory_recoverable_turns", "Received turns with invalid or expired leases", registry=registry)

    def observe_model(self, kind: str, outcome: str, elapsed: float, usage: Any = None) -> None:
        labels = (_kind(kind), _outcome(outcome))
        self.model_calls.labels(*labels).inc()
        self.model_seconds.labels(*labels).observe(_number(elapsed))
        source = _usage(usage)
        aliases = {
            "input": ("input_tokens", "input"), "output": ("output_tokens", "output"),
            "cache_read": ("cache_read_input_tokens", "cacheRead"),
            "cache_write": ("cache_creation_input_tokens", "cacheWrite"),
        }
        for token_type, fields in aliases.items():
            value = next((source[field] for field in fields if source.get(field) is not None), 0)
            self.model_tokens.labels(*labels, token_type).inc(_number(value))

    def observe_job(self, kind: str, outcome: str) -> None:
        self.jobs.labels(_kind(kind), _outcome(outcome)).inc()

    def observe_context(self, rendered_chars: int, truncated: bool) -> None:
        self.context_renders.inc()
        self.context_chars.observe(_number(rendered_chars))
        if truncated:
            self.context_truncations.inc()

    def observe_summary(self, before_chars: int, after_chars: int, elapsed: float, outcome: str) -> None:
        outcome = _outcome(outcome)
        self.summaries.labels(outcome).inc()
        self.summary_chars.labels("before").observe(_number(before_chars))
        self.summary_chars.labels("after").observe(_number(after_chars))
        self.summary_seconds.labels(outcome).observe(_number(elapsed))

    def observe_capacity(self, reason: str) -> None:
        reason = _REASON_ALIASES.get(str(reason).lower(), str(reason).lower())
        self.capacity.labels(reason if reason in REASONS else "other").inc()

    def refresh_repository(self, repository: Any) -> dict[str, Any]:
        status = repository_status(repository)
        self.active_facts.set(status["active_facts"])
        self.active_fact_chars.set(status["active_fact_content_chars"])
        self.pending_sources.set(status["pending_sources"])
        self.oldest_source_seconds.set(status["oldest_source_age_seconds"])
        self.pending_index_deletes.set(status["pending_index_deletes"])
        self.recoverable_turns.set(status["recoverable_turns"])
        # Every bounded combination is reset on refresh, so completed queues do
        # not leave stale gauges. Prometheus collectors synchronize updates.
        for kind in KINDS:
            self.oldest_job_seconds.labels(kind).set(status["oldest_pending_seconds"][kind])
            for state in STATES:
                self.job_queue.labels(kind, state).set(status["jobs"][kind][state])
        return status


def repository_status(repository: Any) -> dict[str, Any]:
    """Return only aggregate numeric operational state, never identities or text."""
    jobs = {kind: {state: 0 for state in STATES} for kind in KINDS}
    ages = {kind: 0.0 for kind in KINDS}
    with repository._connection() as db:
        db.execute("BEGIN")
        params = (repository.namespace,)
        facts = db.execute("SELECT COUNT(*),COALESCE(SUM(length(content)),0) FROM memory_fact WHERE namespace=? AND status='ACTIVE'", params).fetchone()
        rows = db.execute("""SELECT kind,status,COUNT(*) AS count,
            MAX(MAX(0,(julianday('now')-julianday(created_at))*86400)) AS age
            FROM memory_job WHERE namespace=? AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED')
            GROUP BY kind,status""", params).fetchall()
        for row in rows:
            kind, state = _kind(row["kind"]), row["status"].lower()
            jobs[kind][state] += int(row["count"])
            ages[kind] = max(ages[kind], _number(row["age"]))
        sources = db.execute("""SELECT COUNT(*),MAX(MAX(0,(julianday('now')-julianday(t.received_at))*86400))
            FROM memory_turn t JOIN memory_scope s ON t.namespace=s.namespace AND t.user_id=s.user_id
            WHERE t.namespace=? AND t.memory_eligible=1 AND t.deleted_at IS NULL AND s.long_term_enabled=1
            AND lower(trim(t.user_id)) NOT IN ('anonymous','demo') AND s.migration_state!='IMPORTING'
            AND t.source_seq>s.extract_cursor AND t.source_seq>s.context_floor_source_seq""", params).fetchone()
        deletes = db.execute("SELECT COUNT(*) FROM memory_job WHERE namespace=? AND kind='index_delete' AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED')", params).fetchone()[0]
        deletes += db.execute("SELECT COUNT(DISTINCT claim_token) FROM memory_job WHERE namespace=? AND error_code='INDEX_WRITE_UNACKNOWLEDGED'", params).fetchone()[0]
        recoverable = db.execute("""SELECT COUNT(*) FROM memory_turn t LEFT JOIN memory_session s
            ON t.namespace=s.namespace AND t.user_id=s.user_id AND t.conv_id=s.conv_id
            WHERE t.namespace=? AND t.status='RECEIVED' AND (s.lease_until IS NULL
            OR julianday(s.lease_until)<=julianday('now') OR s.lease_owner IS NOT t.request_id OR s.lease_token IS NOT t.lease_token)""", params).fetchone()[0]
        db.commit()
    return {
        "active_facts": int(facts[0]), "active_fact_content_chars": int(facts[1]),
        "jobs": jobs, "oldest_pending_seconds": ages,
        "pending_sources": int(sources[0]) if repository.long_term_enabled else 0,
        "oldest_source_age_seconds": _number(sources[1]) if repository.long_term_enabled else 0.0,
        "quarantined_jobs": sum(values["quarantined"] for values in jobs.values()),
        "pending_index_deletes": int(deletes), "recoverable_turns": int(recoverable),
    }


MEMORY_METRICS = MemoryMetrics()
observe_model = MEMORY_METRICS.observe_model
observe_job = MEMORY_METRICS.observe_job
observe_context = MEMORY_METRICS.observe_context
observe_summary = MEMORY_METRICS.observe_summary
observe_capacity = MEMORY_METRICS.observe_capacity
memory_metrics_snapshot = MEMORY_METRICS.refresh_repository
