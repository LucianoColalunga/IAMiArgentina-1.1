"""Ingesta de webhooks: lo único que hace la API con un mensaje antes de contestar 200.

1. Idempotencia rápida en Redis (SET NX) para cortar reintentos sin tocar la base.
2. Guardado transaccional en PostgreSQL (la unicidad (canal, id) es la garantía real).
3. Publicación en la cola. Si la cola no está disponible, el mensaje igual queda guardado y el
   sweeper lo publica después: la plataforma recibe 200 y no reintenta en vano.

Si PostgreSQL no responde se propaga el error y la API contesta 503: las plataformas que
reintentan (Meta, Telegram) vuelven a mandar el mensaje más tarde.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from iamia.channels.models import InboundMessage, WebhookBatch
from iamia.core.clock import utcnow
from iamia.core.metrics import MESSAGES_DUPLICATED, MESSAGES_RECEIVED, STATUS_UPDATES
from iamia.messaging.contracts import Job
from iamia.persistence.inbox import InboxRepository
from iamia.services.publisher import JobPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: int = 0
    duplicated: int = 0
    stale: int = 0
    statuses: int = 0
    identity_changes: int = 0
    ignored: int = 0


class IngestService:
    def __init__(
        self,
        inbox: InboxRepository,
        publisher: JobPublisher,
        redis: Redis,
        *,
        dedup_ttl_seconds: int = 172_800,
        max_age_seconds: int = 86_400,
    ) -> None:
        self._inbox = inbox
        self._publisher = publisher
        self._redis = redis
        self._dedup_ttl = dedup_ttl_seconds
        self._max_age = max_age_seconds

    async def _first_time(self, message: InboundMessage) -> bool:
        key = f"iamia:seen:{message.channel.value}:{message.provider_message_id}"
        try:
            return bool(await self._redis.set(key, "1", nx=True, ex=self._dedup_ttl))
        except RedisError:
            return True  # sin Redis decide la restricción única de PostgreSQL

    async def _forget(self, message: InboundMessage) -> None:
        try:
            await self._redis.delete(f"iamia:seen:{message.channel.value}:{message.provider_message_id}")
        except RedisError:
            pass

    async def ingest(self, batch: WebhookBatch, *, trace_id: str = "") -> IngestResult:
        accepted, duplicated, stale = 0, 0, 0
        jobs: list[Job] = []
        now = utcnow()
        for message in batch.messages:
            MESSAGES_RECEIVED.labels(message.channel.value, message.kind.value).inc()
            if (now - message.sent_at).total_seconds() > self._max_age:
                stale += 1  # una plataforma que reentrega mensajes de hace días: no se responde
                continue
            if not await self._first_time(message):
                duplicated += 1
                MESSAGES_DUPLICATED.labels(message.channel.value).inc()
                continue
            try:
                stored = await self._inbox.store(message)
            except Exception:
                await self._forget(message)  # que el reintento de la plataforma no se descarte
                raise
            if stored is None:
                duplicated += 1
                MESSAGES_DUPLICATED.labels(message.channel.value).inc()
                continue
            accepted += 1
            jobs.append(Job("inbound", stored.message_id, message.channel.value, trace_id or uuid.uuid4().hex))
        if jobs:
            await self._publisher.publish(jobs)

        applied = 0
        for status in batch.statuses:
            STATUS_UPDATES.labels(status.channel.value, status.state.value).inc()
            if await self._inbox.apply_status(status):
                applied += 1
        changes = 0
        for change in batch.identity_changes:
            if await self._inbox.apply_identity_change(change):
                changes += 1
                logger.info("contact_identity_changed", extra={"ctx": {"channel": change.channel.value}})
        return IngestResult(accepted, duplicated, stale, applied, changes, batch.ignored)
