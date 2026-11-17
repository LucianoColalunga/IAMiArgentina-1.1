"""Recepción de webhooks: idempotencia, mensajes viejos, caídas de la base y de la cola."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from redis.exceptions import RedisError

from iamia.channels.models import (
    Channel,
    DeliveryState,
    IdentityChange,
    InboundMessage,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.clock import utcnow
from iamia.messaging.memory import MemoryJobQueue
from iamia.persistence.inbox import StoredInbound
from iamia.services.ingest import IngestService
from iamia.services.publisher import JobPublisher
from tests.factories import inbound_message


@dataclass
class FakeInbox:
    fail: bool = False
    store_calls: int = 0
    known: set[str] = field(default_factory=set)
    marked: list[str] = field(default_factory=list)

    async def store(self, message: InboundMessage) -> StoredInbound | None:
        self.store_calls += 1
        if self.fail:
            raise ConnectionError("la base no responde")
        if message.provider_message_id in self.known:
            return None  # la restricción única de PostgreSQL
        self.known.add(message.provider_message_id)
        return StoredInbound(f"id-{message.provider_message_id}", "conv-1", "contact-1")

    async def apply_status(self, update: StatusUpdate) -> bool:
        return update.provider_message_id != "desconocido"

    async def apply_identity_change(self, change: IdentityChange) -> bool:
        return True

    async def mark_enqueued(self, ids: list[str]) -> None:
        self.marked.extend(ids)


class BrokenRedis:
    async def set(self, *args: object, **kwargs: object) -> bool:
        raise RedisError("sin conexión")

    async def delete(self, *args: object) -> int:
        raise RedisError("sin conexión")


def _service(redis) -> tuple[IngestService, FakeInbox, MemoryJobQueue]:
    inbox, queue = FakeInbox(), MemoryJobQueue()
    service = IngestService(inbox, JobPublisher(queue, inbox.mark_enqueued), redis, max_age_seconds=3600)  # type: ignore[arg-type]
    return service, inbox, queue


async def test_new_messages_are_stored_and_enqueued(redis) -> None:
    service, inbox, queue = _service(redis)
    batch = WebhookBatch(messages=(inbound_message("p-1"), inbound_message("p-2")), ignored=3)
    result = await service.ingest(batch, trace_id="traza-1")
    assert (result.accepted, result.duplicated, result.ignored) == (2, 0, 3)
    assert [job.message_id for job, _ in queue.published] == ["id-p-1", "id-p-2"]
    assert all(job.trace_id == "traza-1" and job.direction == "inbound" for job, _ in queue.published)
    assert inbox.marked == ["id-p-1", "id-p-2"]

    again = await service.ingest(WebhookBatch(messages=(inbound_message("p-1"),)))
    assert again.duplicated == 1 and inbox.store_calls == 2  # Redis cortó el reintento sin tocar la base


async def test_old_messages_are_not_answered(redis) -> None:
    service, inbox, _ = _service(redis)
    old = inbound_message("v-1", sent_at=utcnow() - timedelta(days=3))
    result = await service.ingest(WebhookBatch(messages=(old,)))
    assert result.stale == 1 and inbox.store_calls == 0


async def test_database_failure_lets_the_platform_retry(redis) -> None:
    service, inbox, _ = _service(redis)
    inbox.fail = True
    with pytest.raises(ConnectionError):
        await service.ingest(WebhookBatch(messages=(inbound_message("f-1"),)))
    inbox.fail = False
    # La marca de "ya visto" se borró: el reintento de la plataforma se procesa.
    assert (await service.ingest(WebhookBatch(messages=(inbound_message("f-1"),)))).accepted == 1


async def test_queue_outage_does_not_lose_messages(redis) -> None:
    service, inbox, queue = _service(redis)
    queue.fail_publish = True
    result = await service.ingest(WebhookBatch(messages=(inbound_message("q-1"),)))
    assert result.accepted == 1 and "q-1" in inbox.known  # guardado: el sweeper lo publica después
    assert inbox.marked == []


async def test_without_redis_the_database_decides() -> None:
    service, inbox, _ = _service(BrokenRedis())
    assert (await service.ingest(WebhookBatch(messages=(inbound_message("r-1"),)))).accepted == 1
    assert (await service.ingest(WebhookBatch(messages=(inbound_message("r-1"),)))).duplicated == 1
    assert inbox.store_calls == 2


async def test_statuses_and_identity_changes(redis) -> None:
    service, _, _ = _service(redis)
    now = utcnow()
    batch = WebhookBatch(
        statuses=(
            StatusUpdate(Channel.WHATSAPP, "wamid-1", DeliveryState.READ, now),
            StatusUpdate(Channel.WHATSAPP, "desconocido", DeliveryState.DELIVERED, now),
        ),
        identity_changes=(IdentityChange(Channel.WHATSAPP, "viejo", "nuevo", now),),
    )
    result = await service.ingest(batch)
    assert (result.statuses, result.identity_changes, result.accepted) == (1, 1, 0)
