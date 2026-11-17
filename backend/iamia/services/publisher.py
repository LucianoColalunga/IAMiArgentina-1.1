"""Publicación de trabajos con registro en la bandeja de salida.

Primero se guarda el mensaje (ya hecho por quien llama), después se publica y, si el broker
confirma, se marca `enqueued_at`. Si la publicación falla no se propaga el error: el mensaje
queda guardado sin marcar y el sweeper lo republica.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

from iamia.messaging.contracts import Job, JobQueue, QueueUnavailable

logger = logging.getLogger(__name__)

MarkEnqueued = Callable[[list[str]], Awaitable[None]]


class JobPublisher:
    def __init__(self, queue: JobQueue, mark_enqueued: MarkEnqueued) -> None:
        self._queue = queue
        self._mark = mark_enqueued

    async def publish(self, jobs: Sequence[Job]) -> int:
        published: list[str] = []
        for job in jobs:
            try:
                await self._queue.publish(job)
            except QueueUnavailable as exc:
                logger.warning(
                    "publish_deferred_to_sweeper", extra={"ctx": {"message_id": job.message_id, "error": str(exc)}}
                )
                continue
            published.append(job.message_id)
        if published:
            try:
                await self._mark(published)
            except Exception as exc:
                logger.warning("mark_enqueued_failed", extra={"ctx": {"error": type(exc).__name__}})
        return len(published)
