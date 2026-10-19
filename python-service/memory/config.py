"""Validated settings for the memory features implemented by this service."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(value: str, name: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


@dataclass(frozen=True)
class MemorySettings:
    db_path: str
    namespace: str = "lumenmind"
    long_term_enabled: bool = True
    context_max_chars: int = 24_000
    lease_seconds: int = 90
    recovery_poll_seconds: float = 5.0
    summary_trigger_chars: int = 18000
    summary_trigger_turns: int = 12
    recent_min_turns: int = 3
    summary_max_chars: int = 4000
    fact_block_max_chars: int = 3000
    episode_block_max_chars: int = 3000
    fact_max_count: int = 50
    fact_max_chars: int = 300
    extract_min_messages: int = 3
    extract_max_messages: int = 20
    extract_max_chars: int = 12000
    extract_max_wait_seconds: int = 60
    model_timeout_seconds: int = 30
    job_lease_seconds: int = 180
    episodic_enabled: bool = True

    @classmethod
    def from_env(cls, project_root: Path, env: Mapping[str, str] | None = None) -> "MemorySettings":
        values = os.environ if env is None else env
        raw_path = values.get("MEMORY_DB_PATH", "data/memory/memory.sqlite3").strip()
        if not raw_path:
            raise ValueError("MEMORY_DB_PATH must not be empty")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = project_root / path
        namespace = values.get("MEMORY_NAMESPACE", "lumenmind").strip()
        if not namespace or len(namespace) > 128:
            raise ValueError("MEMORY_NAMESPACE must contain 1 to 128 characters")
        try:
            poll = float(values.get("MEMORY_WORKER_POLL_SECONDS", "5"))
        except ValueError as error:
            raise ValueError("MEMORY_WORKER_POLL_SECONDS must be a number") from error
        if not math.isfinite(poll) or not 0.1 <= poll <= 300:
            raise ValueError("MEMORY_WORKER_POLL_SECONDS must be between 0.1 and 300")
        return cls(
            db_path=str(path.resolve()),
            namespace=namespace,
            long_term_enabled=_boolean(values.get("MEMORY_LONG_TERM_ENABLED", "true"), "MEMORY_LONG_TERM_ENABLED"),
            context_max_chars=_integer(values.get("MEMORY_CONTEXT_MAX_CHARS", "24000"), "MEMORY_CONTEXT_MAX_CHARS", 1000, 1_000_000),
            lease_seconds=_integer(values.get("MEMORY_SESSION_LEASE_SECONDS", "90"), "MEMORY_SESSION_LEASE_SECONDS", 5, 3600),
            recovery_poll_seconds=poll,
            summary_trigger_chars=_integer(values.get("MEMORY_SUMMARY_TRIGGER_CHARS", "18000"), "MEMORY_SUMMARY_TRIGGER_CHARS", 1000, 1000000),
            summary_trigger_turns=_integer(values.get("MEMORY_SUMMARY_TRIGGER_TURNS", "12"), "MEMORY_SUMMARY_TRIGGER_TURNS", 3, 100),
            recent_min_turns=_integer(values.get("MEMORY_RECENT_MIN_TURNS", "3"), "MEMORY_RECENT_MIN_TURNS", 1, 20),
            summary_max_chars=_integer(values.get("MEMORY_SUMMARY_MAX_CHARS", "4000"), "MEMORY_SUMMARY_MAX_CHARS", 256, 4000),
            fact_block_max_chars=_integer(values.get("MEMORY_FACT_BLOCK_MAX_CHARS", "3000"), "MEMORY_FACT_BLOCK_MAX_CHARS", 256, 3000),
            episode_block_max_chars=_integer(values.get("MEMORY_EPISODE_BLOCK_MAX_CHARS", "3000"), "MEMORY_EPISODE_BLOCK_MAX_CHARS", 0, 3000),
            fact_max_count=_integer(values.get("MEMORY_FACT_MAX_COUNT", "50"), "MEMORY_FACT_MAX_COUNT", 1, 50),
            fact_max_chars=_integer(values.get("MEMORY_FACT_MAX_CHARS", "300"), "MEMORY_FACT_MAX_CHARS", 1, 300),
            extract_min_messages=_integer(values.get("MEMORY_EXTRACT_MIN_MESSAGES", "3"), "MEMORY_EXTRACT_MIN_MESSAGES", 1, 20),
            extract_max_messages=_integer(values.get("MEMORY_EXTRACT_MAX_MESSAGES", "20"), "MEMORY_EXTRACT_MAX_MESSAGES", 1, 20),
            extract_max_chars=_integer(values.get("MEMORY_EXTRACT_MAX_CHARS", "12000"), "MEMORY_EXTRACT_MAX_CHARS", 1000, 20000),
            extract_max_wait_seconds=_integer(values.get("MEMORY_EXTRACT_MAX_WAIT_SECONDS", "60"), "MEMORY_EXTRACT_MAX_WAIT_SECONDS", 1, 3600),
            model_timeout_seconds=_integer(values.get("MEMORY_MODEL_TIMEOUT_SECONDS", "30"), "MEMORY_MODEL_TIMEOUT_SECONDS", 1, 120),
            job_lease_seconds=_integer(values.get("MEMORY_JOB_LEASE_SECONDS", "180"), "MEMORY_JOB_LEASE_SECONDS", 10, 3600),
            episodic_enabled=_boolean(values.get("MEMORY_EPISODIC_ENABLED", "true"), "MEMORY_EPISODIC_ENABLED"),
        )
