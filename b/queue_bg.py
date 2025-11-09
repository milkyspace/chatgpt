from __future__ import annotations
import asyncio
from typing import Callable, Awaitable, Any

class AsyncWorkerPool:
    """Простой пул воркеров. Кладем задачи в очередь — обрабатываются в фоне."""
    def __init__(self, workers: int):
        self.queue: asyncio.Queue[Callable[[], Awaitable[Any]]] = asyncio.Queue()
        self.workers = workers
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        for _ in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker()))

    async def _worker(self):
        while True:
            job = await self.queue.get()
            try:
                await job()
            except Exception:
                # логируем/метрики — опущено
                pass
            finally:
                self.queue.task_done()

    async def submit(self, coro_factory: Callable[[], Awaitable[Any]]):
        """Добавляет задачу (ленивую фабрику корутины) в очередь."""
        await self.queue.put(coro_factory)

    async def stop(self):
        for t in self._tasks:
            t.cancel()
