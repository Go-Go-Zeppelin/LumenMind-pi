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
        )
