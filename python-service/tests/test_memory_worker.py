"""The running worker recovers turns, never fabricates extraction completion."""
import asyncio
import threading

import pytest

from memory.worker import MemoryWorker


def test_recovery_runs_off_event_loop_and_stop_wakes_poll():
    async def scenario():
        loop_thread = threading.get_ident()
        recovered = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Repository:
            calls = 0

            def recover_interrupted_turns(self):
                assert threading.get_ident() != loop_thread
                self.calls += 1
                loop.call_soon_threadsafe(recovered.set)

            def claim_due_jobs(self):
                pytest.fail("Recovery-only worker must not claim unimplemented fact jobs")

        repository = Repository()
        worker = MemoryWorker(repository, poll_seconds=300)
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(recovered.wait(), timeout=2)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)
        assert repository.calls == 1

    asyncio.run(scenario())


def test_recovery_retries_after_storage_failure():
    async def scenario():
        recovered = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Repository:
            calls = 0

            def recover_interrupted_turns(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("temporarily unavailable")
                loop.call_soon_threadsafe(recovered.set)

        repository = Repository()
        worker = MemoryWorker(repository, poll_seconds=0.01)
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(recovered.wait(), timeout=2)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)
        assert repository.calls >= 2

    asyncio.run(scenario())


@pytest.mark.parametrize("poll", [0, -1, float("inf"), float("nan")])
def test_worker_rejects_invalid_poll(poll):
    with pytest.raises(ValueError):
        MemoryWorker(object(), poll_seconds=poll)
