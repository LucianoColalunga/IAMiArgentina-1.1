"""Cola de trabajos sobre RabbitMQ real: topología, reintentos con espera, DLQ y contrapresión."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import aio_pika
import pytest

from iamia.messaging.contracts import Job, QueueUnavailable
from iamia.messaging.rabbitmq import RabbitJobQueue, queue_name, work_routing_keys
from tests.integration.conftest import AMQP_URL


async def _purge_all() -> None:
    connection = await aio_pika.connect_robust(AMQP_URL)
    async with connection:
        channel = await connection.channel()
        for key in [*work_routing_keys(), "dead.inbound", "dead.outbound"]:
            try:
                queue = await channel.get_queue(queue_name(key), ensure=True)
                await queue.purge()
            except aio_pika.exceptions.ChannelClosed:
                channel = await connection.channel()


@pytest.fixture
async def rabbit() -> AsyncIterator[RabbitJobQueue]:
    queue = RabbitJobQueue(AMQP_URL, max_length=1000)
    await queue.connect()
    await _purge_all()
    yield queue
    await queue.close()


async def _collect(queue: RabbitJobQueue, routing_key: str, count: int, wait_seconds: float = 10.0) -> list[Job]:
    received: list[Job] = []
    done = asyncio.Event()

    async def handler(job: Job) -> None:
        received.append(job)
        if len(received) >= count:
            done.set()

    consumer = asyncio.create_task(queue.consume(routing_key, handler, prefetch=4))
    try:
        await asyncio.wait_for(done.wait(), wait_seconds)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    return received


async def test_publish_and_consume(rabbit: RabbitJobQueue) -> None:
    await rabbit.publish(Job("inbound", "m-1", "telegram", "t-1"))
    await rabbit.publish(Job("outbound", "m-2", "whatsapp", "t-2"))
    inbound = await _collect(rabbit, "inbound", 1)
    outbound = await _collect(rabbit, "outbound.whatsapp", 1)
    assert inbound[0].message_id == "m-1" and inbound[0].trace_id == "t-1"
    assert outbound[0].channel == "whatsapp"
    assert await rabbit.ping()


async def test_delayed_retry_comes_back_after_its_tier(rabbit: RabbitJobQueue) -> None:
    started = time.monotonic()
    await rabbit.publish(Job("outbound", "m-3", "telegram").retried(), delay_seconds=1)
    jobs = await _collect(rabbit, "outbound.telegram", 1, wait_seconds=15)
    elapsed = time.monotonic() - started
    assert jobs[0].message_id == "m-3" and jobs[0].retries == 1
    assert elapsed >= 0.9


async def test_failed_handler_is_retried_after_a_wait(rabbit: RabbitJobQueue) -> None:
    await rabbit.publish(Job("inbound", "m-4", "telegram"))
    attempts: list[tuple[int, float]] = []
    done = asyncio.Event()

    async def flaky(job: Job) -> None:
        attempts.append((job.retries, time.monotonic()))
        if len(attempts) == 1:
            raise ConnectionResetError("la base se reinicia")
        done.set()

    consumer = asyncio.create_task(rabbit.consume("inbound", flaky, prefetch=1))
    try:
        await asyncio.wait_for(done.wait(), 15)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    # Vuelve una sola vez y después de esperar el primer nivel (1 s), no en un bucle inmediato.
    assert [retries for retries, _ in attempts] == [0, 1]
    assert attempts[1][1] - attempts[0][1] >= 0.9


async def test_handler_failures_end_in_the_dlq() -> None:
    queue = RabbitJobQueue(AMQP_URL, max_length=1000, handler_retries=1)
    await queue.connect()
    await _purge_all()
    attempts: list[int] = []

    async def broken(job: Job) -> None:
        attempts.append(job.retries)
        raise RuntimeError("error del programa")

    try:
        await queue.publish(Job("inbound", "m-6", "telegram"))
        consumer = asyncio.create_task(queue.consume("inbound", broken, prefetch=1))
        try:
            for _ in range(75):
                if await queue.depth("dead.inbound") == 1:
                    break
                await asyncio.sleep(0.2)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        assert attempts == [0, 1]
        assert await queue.depth("dead.inbound") == 1
    finally:
        await _purge_all()
        await queue.close()


async def test_dead_letter_goes_to_the_dlq(rabbit: RabbitJobQueue) -> None:
    await rabbit.dead_letter(Job("outbound", "m-5", "sms"), "auth:190")
    for _ in range(20):
        if await rabbit.depth("dead.outbound") == 1:
            break
        await asyncio.sleep(0.2)
    assert await rabbit.depth("dead.outbound") == 1


async def test_full_queue_rejects_publish() -> None:
    small = RabbitJobQueue(AMQP_URL, max_length=1000)
    await small.connect()
    await _purge_all()
    try:
        # Cola con x-max-length=1000 y reject-publish. Las colas quorum rechazan cuando el largo ya
        # superó el límite, así que aceptan uno más (medido: 1001) y desde ahí el broker hace nack.
        for index in range(1001):
            await small.publish(Job("outbound", f"f-{index}", "email"))
        for _ in range(3):
            with pytest.raises(QueueUnavailable):
                await small.publish(Job("outbound", "f-overflow", "email"))
        assert await small.depth("outbound.email") == 1001
    finally:
        await _purge_all()
        await small.close()
