"""Nonblocking model-driven rolling summary execution."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .metrics import observe_summary, observe_job
from dataclasses import dataclass
from pathlib import Path

from .repository import ConflictError, LeaseError

logger = logging.getLogger(__name__)
_PROMPT = Path(__file__).parent.parent / "prompts" / "summary.md"


@dataclass(frozen=True)
class SummaryServiceSettings:
    trigger_chars: int = 18_000
    trigger_turns: int = 12
    recent_min_turns: int = 3
    max_batch_chars: int = 16_000
    max_summary_chars: int = 4_000
    lease_seconds: int = 180
    max_attempts: int = 3
    max_concurrency: int = 4

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in self.__dict__.values()):
            raise ValueError("Summary settings must be positive integers")


class SummaryService:
    def __init__(self, repository, model, settings: SummaryServiceSettings = SummaryServiceSettings()):
        self.repository = repository
        self.model = model
        self.settings = settings
        self._tasks: dict[str, asyncio.Task] = {}
        self._claim_lock = asyncio.Lock()
        self._closed = False
        self._system = _PROMPT.read_text(encoding="utf-8")

    async def _process(self, job):
        started = time.perf_counter()
        try:
            # Status + role fields keep failed/interrupted work from being
            # rewritten into a fabricated assistant or tool success.
            prompt = json.dumps({"schema_version": 1, "previous_summary": job["previous_summary"],
                "complete_turns": job["turns"], "max_summary_chars": self.settings.max_summary_chars},
                ensure_ascii=False, separators=(",", ":"))
            payload = await self.model.generate_json(self._system, prompt)
            result = await asyncio.to_thread(self.repository.apply_summary_result,
                job["job_id"], job["claim_token"], payload, max_chars=self.settings.max_summary_chars)
            before = len(json.dumps(job["previous_summary"], ensure_ascii=False)) + sum(len(turn["user_text"]) + len(turn.get("assistant_text") or "") for turn in job["turns"])
            observe_summary(before, len(json.dumps(payload, ensure_ascii=False)), time.perf_counter() - started, "success")
            observe_job("summary", "WRITTEN")
            return result
        except (ConflictError, LeaseError) as error:
            return await asyncio.to_thread(self.repository.fail_summary_job,
                job["job_id"], job["claim_token"], type(error).__name__, conflict=True,
                max_attempts=self.settings.max_attempts)
        except asyncio.CancelledError:
            raise  # Durable claim becomes reclaimable; old summary stays intact.
        except Exception as error:
            observe_summary(0, 0, time.perf_counter() - started, "failed")
            observe_job("summary", "failed")
            logger.warning("Summary job %s failed: %s", job["job_id"], type(error).__name__)
            return await asyncio.to_thread(self.repository.fail_summary_job,
                job["job_id"], job["claim_token"], type(error).__name__, conflict=False,
                max_attempts=self.settings.max_attempts)

    def _done(self, job_id, task):
        self._tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Summary job %s could not persist its result: %s", job_id, type(task.exception()).__name__)

    async def tick(self) -> None:
        if self._closed:
            return
        await asyncio.to_thread(self.repository.schedule_due_summary_jobs,
            trigger_chars=self.settings.trigger_chars, trigger_turns=self.settings.trigger_turns,
            recent_min_turns=self.settings.recent_min_turns, max_batch_chars=self.settings.max_batch_chars)
        async with self._claim_lock:
            if self._closed or len(self._tasks) >= self.settings.max_concurrency:
                return
            job = await asyncio.to_thread(self.repository.claim_summary_job, lease_seconds=self.settings.lease_seconds)
            if job is None:
                return
            task = asyncio.create_task(self._process(job), name=f"memory-summary-{job['job_id']}")
            self._tasks[job["job_id"]] = task
            task.add_done_callback(lambda completed: self._done(job["job_id"], completed))

    async def close(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        await self.model.close()
