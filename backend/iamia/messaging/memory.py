"""Cola en memoria: pruebas y modo de un solo proceso. No es durable."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict

from iamia.messaging.contracts import Job, JobHandler, QueueUnavailable
from iamia.messaging.failures import HANDLER_RETRIES, handler_retry_delay, log_handler_error

logger = logging.getLogger(__name__)


class MemoryJobQueue:
    def __init__(self, *, max_length: int = 100_000) -> None:
        self._queues: dict[str, asyncio.Queue[Job]] = defaultdict(lambda: asyncio.Queue(max_length))
        self._timers: set[asyncio.TimerHandle] = set()
        self.dead: list[tuple[Job, str]] = []
        self.published: list[tuple[Job, float]] = []
        self.fail_publish = False  # para simular un broker caído en pruebas

    def _put(self, job: Job) -> None:
        try:
            self._queues[job.routing_key].put_nowait(job)
        except asyncio.QueueFull:
            logger.error("memory_queue_full", extra={"ctx": {"queue": job.routing_key}})

    async def publish(self, job: Job, *, delay_seconds: float = 0.0) -> None:
        if self.fail_publish:
            raise QueueUnavailable("cola en memoria marcada como caída")
        self.published.append((job, delay_seconds))
        if delay_seconds > 0:
            loop = asyncio.get_running_loop()
            handle = loop.call_later(delay_seconds, self._put, job)
            self._timers.add(handle)
            return
        queue = self._queues[job.routing_key]
        if queue.full():
            raise QueueUnavailable("cola en memoria llena")
        queue.put_nowait(job)

    async def dead_letter(self, job: Job, reason: str) -> None:
        self.dead.append((job, reason))

    async def consume(self, routing_key: str, handler: JobHandler, *, prefetch: int) -> None:
        queue = self._queues[routing_key]
        slots = asyncio.Semaphore(max(1, prefetch))
        running: set[asyncio.Task[None]] = set()

        async def run(job: Job) -> None:
            try:
                await handler(job)
            except Exception as exc:
                log_handler_error(job, exc)
                if job.retries >= HANDLER_RETRIES:
                    self.dead.append((job, f"handler:{type(exc).__name__}"))
                    return
                handle = asyncio.get_running_loop().call_later(handler_retry_delay(job), self._put, job.retried())
                self._timers.add(handle)
            finally:
                slots.release()

        try:
            while True:
                job = await queue.get()
                await slots.acquire()
                task = asyncio.create_task(run(job))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            if running:
                await asyncio.wait(running, timeout=10)

    async def depth(self, routing_key: str) -> int | None:
        return self._queues[routing_key].qsize()

    async def ping(self) -> bool:
        return not self.fail_publish

    async def close(self) -> None:
        for handle in self._timers:
            with contextlib.suppress(Exception):
                handle.cancel()
        self._timers.clear()
