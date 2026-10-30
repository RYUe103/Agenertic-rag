from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import AsyncIterator


class AskConcurrency:
    """Bound expensive Agent/LLM work without blocking the event loop."""

    def __init__(self, limit: int = 40, queue_timeout: float = 10.0) -> None:
        self.limit = max(1, limit)
        self.queue_timeout = max(0.0, queue_timeout)
        self._slots = asyncio.Semaphore(self.limit)
        self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="ask")
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()
        self.active = 0
        self.rejected = 0

    async def _conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        async with self._lock_guard:
            return self._conversation_locks.setdefault(conversation_id, asyncio.Lock())

    @asynccontextmanager
    async def acquire(self, conversation_id: str) -> AsyncIterator[None]:
        # Serialize one conversation before taking a worker slot; follow-up
        # requests must not consume all capacity while waiting on each other.
        lock = await self._conversation_lock(conversation_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.queue_timeout)
        except TimeoutError:
            self.rejected += 1
            raise
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.queue_timeout)
        except TimeoutError:
            self.rejected += 1
            lock.release()
            raise
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            lock.release()
            self._slots.release()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
