"""Cola de trabajos sobre RabbitMQ (colas quorum, replicadas).

Topología:
- iamia.jobs (direct) -> iamia.inbound y iamia.outbound.<canal> (una cola por canal: si un
  canal se atrasa o su proveedor cae, los demás siguen saliendo).
- Reintentos con espera: se publica en iamia.retry (topic) con clave "<nivel>.<destino>"; cada
  nivel es una cola con TTL fijo (1 s, 5 s, 30 s, 2 min, 10 min, 30 min) que al vencer devuelve
  el trabajo a su cola original por iamia.requeue. Sin plugins.
- Descarte: iamia.dead -> iamia.dead.inbound / iamia.dead.outbound (DLQ) cuando se agotan los
  intentos o un mensaje se reentrega demasiadas veces (x-delivery-limit).
- Trabajo que falla con una excepción no prevista (por ejemplo, PostgreSQL reiniciándose): vuelve
  por esos mismos niveles de espera y, agotados los intentos, va a la DLQ (ver failures.py).
- Contrapresión: x-max-length con x-overflow=reject-publish. Si la cola se llena el broker
  rechaza la publicación, el mensaje queda guardado en PostgreSQL y el sweeper lo republica
  cuando hay lugar: no se pierde y la API sigue respondiendo a las plataformas.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage, AbstractRobustConnection

from iamia.channels.models import Channel
from iamia.core.metrics import QUEUE_DEAD, QUEUE_PUBLISHED, QUEUE_RETRIED
from iamia.messaging.contracts import Job, JobHandler, QueueUnavailable
from iamia.messaging.failures import HANDLER_RETRIES, handler_retry_delay, log_handler_error

logger = logging.getLogger(__name__)

JOBS_EXCHANGE = "iamia.jobs"
RETRY_EXCHANGE = "iamia.retry"
REQUEUE_EXCHANGE = "iamia.requeue"
DEAD_EXCHANGE = "iamia.dead"
RETRY_TIERS = (1, 5, 30, 120, 600, 1800)
_PUBLISH_TIMEOUT = 5.0


def queue_name(routing_key: str) -> str:
    return f"iamia.{routing_key}"


def retry_tier(delay_seconds: float) -> int:
    for tier in RETRY_TIERS:
        if delay_seconds <= tier:
            return tier
    return RETRY_TIERS[-1]


def work_routing_keys() -> list[str]:
    return ["inbound", *(f"outbound.{channel.value}" for channel in Channel)]


class RabbitJobQueue:
    def __init__(
        self, url: str, *, max_length: int = 500_000, delivery_limit: int = 10, handler_retries: int = HANDLER_RETRIES
    ) -> None:
        self._url = url
        self._max_length = max_length
        self._delivery_limit = delivery_limit
        self._handler_retries = handler_retries
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchanges: dict[str, AbstractExchange] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._channel is not None and not self._channel.is_closed:
                return
            self._connection = await aio_pika.connect_robust(self._url, client_properties={"connection_name": "iamia"})
            self._channel = await self._connection.channel(publisher_confirms=True)
            await self._declare(self._channel)

    async def _declare(self, channel: AbstractChannel) -> None:
        durable = {"durable": True}
        jobs = await channel.declare_exchange(JOBS_EXCHANGE, aio_pika.ExchangeType.DIRECT, **durable)
        retry = await channel.declare_exchange(RETRY_EXCHANGE, aio_pika.ExchangeType.TOPIC, **durable)
        requeue = await channel.declare_exchange(REQUEUE_EXCHANGE, aio_pika.ExchangeType.TOPIC, **durable)
        dead = await channel.declare_exchange(DEAD_EXCHANGE, aio_pika.ExchangeType.DIRECT, **durable)
        self._exchanges = {JOBS_EXCHANGE: jobs, RETRY_EXCHANGE: retry, DEAD_EXCHANGE: dead}

        for routing_key in work_routing_keys():
            direction = routing_key.split(".", 1)[0]
            queue = await channel.declare_queue(
                queue_name(routing_key),
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-delivery-limit": self._delivery_limit,
                    "x-max-length": self._max_length,
                    "x-overflow": "reject-publish",
                    "x-dead-letter-exchange": DEAD_EXCHANGE,
                    "x-dead-letter-routing-key": direction,
                    "x-dead-letter-strategy": "at-least-once",
                },
            )
            await queue.bind(jobs, routing_key)
            await queue.bind(requeue, f"*.{routing_key}")

        for tier in RETRY_TIERS:
            queue = await channel.declare_queue(
                f"iamia.retry.{tier}s",
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-message-ttl": tier * 1000,
                    "x-dead-letter-exchange": REQUEUE_EXCHANGE,
                    "x-dead-letter-strategy": "at-least-once",
                    "x-overflow": "reject-publish",
                    "x-max-length": self._max_length,
                },
            )
            await queue.bind(retry, f"{tier}s.#")

        for direction in ("inbound", "outbound"):
            queue = await channel.declare_queue(
                f"iamia.dead.{direction}", durable=True, arguments={"x-queue-type": "quorum"}
            )
            await queue.bind(dead, direction)

    async def _exchange(self, name: str) -> AbstractExchange:
        if self._channel is None or self._channel.is_closed:
            await self.connect()
        return self._exchanges[name]

    @staticmethod
    def _message(job: Job, **headers: Any) -> aio_pika.Message:
        return aio_pika.Message(
            job.to_bytes(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=f"{job.message_id}:{job.retries}",
            headers={"x-trace-id": job.trace_id, **headers},
        )

    async def _publish(self, exchange_name: str, job: Job, routing_key: str, **headers: Any) -> None:
        try:
            exchange = await self._exchange(exchange_name)
            await exchange.publish(self._message(job, **headers), routing_key, mandatory=True, timeout=_PUBLISH_TIMEOUT)
        except Exception as exc:
            raise QueueUnavailable(f"no se pudo publicar en {routing_key}: {type(exc).__name__}") from exc

    async def publish(self, job: Job, *, delay_seconds: float = 0.0) -> None:
        if delay_seconds <= 0:
            await self._publish(JOBS_EXCHANGE, job, job.routing_key)
            QUEUE_PUBLISHED.labels(job.routing_key).inc()
            return
        tier = retry_tier(delay_seconds)
        await self._publish(RETRY_EXCHANGE, job, f"{tier}s.{job.routing_key}")
        QUEUE_RETRIED.labels(job.routing_key).inc()

    async def dead_letter(self, job: Job, reason: str) -> None:
        await self._publish(DEAD_EXCHANGE, job, job.direction, **{"x-reason": reason[:120]})
        QUEUE_DEAD.labels(job.routing_key).inc()

    async def consume(self, routing_key: str, handler: JobHandler, *, prefetch: int) -> None:
        if self._connection is None:
            await self.connect()
        assert self._connection is not None
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=max(1, prefetch))
        queue = await channel.get_queue(queue_name(routing_key), ensure=True)
        slots = asyncio.Semaphore(max(1, prefetch))
        running: set[asyncio.Task[None]] = set()
        try:
            async with queue.iterator() as messages:
                async for message in messages:
                    await slots.acquire()
                    task = asyncio.create_task(self._run(message, handler, slots))
                    running.add(task)
                    task.add_done_callback(running.discard)
        finally:
            if running:  # apagado ordenado: se terminan los trabajos en curso
                await asyncio.wait(running, timeout=30)
            with contextlib.suppress(Exception):
                await channel.close()

    async def _run(self, message: AbstractIncomingMessage, handler: JobHandler, slots: asyncio.Semaphore) -> None:
        try:
            try:
                job = Job.from_bytes(message.body)
            except (ValueError, KeyError, TypeError):
                logger.error("queue_invalid_job")
                await message.reject(requeue=False)  # va a la DLQ
                return
            try:
                await handler(job)
            except asyncio.CancelledError:
                await message.nack(requeue=True)
                raise
            except Exception as exc:
                log_handler_error(job, exc)
                await self._retry_later(message, job)
                return
            await message.ack()
        finally:
            slots.release()

    async def _retry_later(self, message: AbstractIncomingMessage, job: Job) -> None:
        if job.retries >= self._handler_retries:
            await message.reject(requeue=False)  # DLQ: queda visible en las métricas y la consola
            return
        try:
            await self.publish(job.retried(), delay_seconds=handler_retry_delay(job))
        except QueueUnavailable:
            await message.nack(requeue=True)  # sin broker para la espera: queda la reentrega común
            return
        await message.ack()

    async def depth(self, routing_key: str) -> int | None:
        try:
            if self._connection is None:
                await self.connect()
            assert self._connection is not None
            channel = await self._connection.channel()
            try:
                queue = await channel.declare_queue(queue_name(routing_key), passive=True)
                return int(queue.declaration_result.message_count or 0)
            finally:
                await channel.close()
        except Exception:
            return None

    async def ping(self) -> bool:
        try:
            await self._exchange(JOBS_EXCHANGE)
        except Exception:
            return False
        return self._connection is not None and not self._connection.is_closed

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._channel = None
