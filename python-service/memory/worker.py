"""可恢复持久任务 Worker 的最小调度外壳。

Worker 不依赖 FastAPI BackgroundTasks；进程重启后由 repository 扫描 READY/
RETRY_WAIT 任务即可继续。实际 Judge/摘要器通过函数注入，便于离线测试。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional


class MemoryWorker:
    def __init__(self, repository, poll_seconds: float = 5.0):
        self.repository = repository
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            await asyncio.to_thread(self.repository.recover_interrupted_turns)
            jobs = await asyncio.to_thread(self.repository.claim_due_jobs)
            for job in jobs:
                try:
                    await handler(job)
                    await asyncio.to_thread(self.repository.complete_job, job["job_id"], job["claim_token"], {"status": "DONE"})
                except Exception as error:
                    await asyncio.to_thread(self.repository.retry_job, job["job_id"], job["claim_token"], str(error))
            try: await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError: pass

