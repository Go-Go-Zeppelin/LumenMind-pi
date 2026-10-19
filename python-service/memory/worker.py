"""Recovery scheduler for expired turn leases.

Fact extraction, summaries and index jobs are deliberately not claimed here:
there is no installed model pipeline that can safely complete those jobs yet.
"""
from __future__ import annotations

import asyncio
import logging
import math

logger = logging.getLogger(__name__)


class MemoryWorker:
    def __init__(self, repository, poll_seconds: float = 5.0):
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a positive finite number")
        self.repository = repository
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                # SQLite work is blocking; keep the API event loop available.
                await asyncio.to_thread(self.repository.recover_interrupted_turns)
            except Exception:
                logger.exception("Memory lease recovery failed; retrying on next poll")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
