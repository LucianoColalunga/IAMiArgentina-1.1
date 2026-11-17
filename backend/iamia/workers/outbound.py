"""Dispatcher de salida: envía cada respuesta por su canal respetando los límites del proveedor.

Por cada canal hay una cola, un circuit breaker y un throttle propios: si WhatsApp devuelve
errores o se queda sin cupo, Telegram y el resto siguen funcionando.

Clasificación de fallas (DeliveryError.kind):
- RATE_LIMITED: se reprograma después de lo que pidió el proveedor.
- RETRYABLE: cuenta para el circuit breaker y se reintenta con backoff exponencial + jitter.
- AUTH: credenciales o cuenta con problemas; se abre el circuito del canal y se alerta.
- WINDOW_CLOSED / PERMANENT: no se reintenta (queda como fallido en la consola).
Agotados los intentos el mensaje pasa a "dead" y a la DLQ para revisión manual.

Las respuestas largas se parten en varias partes; el progreso se guarda para que un reintento no
repita las partes ya enviadas.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from opentelemetry.trace import SpanKind

from iamia.channels.models import Channel, DeliveryError, FailureKind, SendContext
from iamia.channels.registry import ChannelRegistry
from iamia.conversations.models import Author, OutboundStatus, OutboundWork
from iamia.core.clock import utcnow
from iamia.core.logs import log_context
from iamia.core.metrics import DELIVERIES, END_TO_END_SECONDS
from iamia.core.resilience import Backoff, CircuitBreaker
from iamia.core.tracing import child_span, root_span
from iamia.messaging.contracts import Job, JobQueue, QueueUnavailable
from iamia.messaging.throttle import ChannelThrottle
from iamia.persistence.work import WorkRepository

logger = logging.getLogger(__name__)

_MAX_INLINE_WAIT = 1.5  # esperas cortas del throttle se hacen en el proceso; las largas, en la cola


@dataclass(frozen=True)
class OutboundConfig:
    lease_seconds: int = 120
    max_attempts: int = 8


class OutboundDispatcher:
    def __init__(
        self,
        *,
        work: WorkRepository,
        registry: ChannelRegistry,
        queue: JobQueue,
        breakers: Mapping[Channel, CircuitBreaker],
        throttles: Mapping[Channel, ChannelThrottle],
        config: OutboundConfig | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        self._work = work
        self._registry = registry
        self._queue = queue
        self._breakers = breakers
        self._throttles = throttles
        self._config = config or OutboundConfig()
        self._backoff = backoff or Backoff(base=2.0, factor=3.0, max_delay=1800.0)

    async def handle(self, job: Job) -> None:
        with (
            log_context(message_id=job.message_id, channel=job.channel, trace_id=job.trace_id),
            root_span(
                "outbound.deliver",
                job.trace_id,
                kind=SpanKind.CONSUMER,
                **{"iamia.channel": job.channel, "iamia.message_id": job.message_id, "iamia.retries": job.retries},
            ),
        ):
            work = await self._work.claim_outbound(job.message_id, self._config.lease_seconds)
            if work is None:
                return
            adapter = self._registry.find(work.channel.value)
            if adapter is None:
                await self._work.finish_outbound(work.message_id, OutboundStatus.FAILED, error_code="channel_disabled")
                DELIVERIES.labels(work.channel.value, "channel_disabled").inc()
                return
            breaker = self._breakers.get(work.channel)
            if breaker is not None and not breaker.allow():
                await self._defer(
                    job, work, max(1.0, breaker.retry_after()), count_attempt=False, reason="circuit_open"
                )
                return
            if not await self._wait_turn(job, work):
                return
            await self._deliver(job, work, adapter, breaker)

    async def _wait_turn(self, job: Job, work: OutboundWork) -> bool:
        throttle = self._throttles.get(work.channel)
        if throttle is None:
            return True
        wait = await throttle.acquire()
        if wait > _MAX_INLINE_WAIT:
            await self._defer(job, work, wait, count_attempt=False, reason="throttled")
            return False
        if wait > 0:
            await asyncio.sleep(wait)
            if await throttle.acquire() > 0:
                await self._defer(job, work, _MAX_INLINE_WAIT, count_attempt=False, reason="throttled")
                return False
        return True

    async def _deliver(self, job: Job, work: OutboundWork, adapter, breaker: CircuitBreaker | None) -> None:
        parts = adapter.formatter.render(work.content)
        context = SendContext(now=utcnow(), operator_reply=work.author is Author.OPERATOR, message_id=work.message_id)
        provider_ids = list(work.provider_ids)
        sent = work.parts_sent
        try:
            for index in range(work.parts_sent, len(parts)):
                if index > work.parts_sent and not await self._wait_part(work.channel):
                    raise DeliveryError(FailureKind.RATE_LIMITED, "límite del canal entre partes", retry_after=2.0)
                with child_span("channel.send", kind=SpanKind.CLIENT, **{"iamia.channel": work.channel.value}):
                    result = await adapter.sender.send(work.recipient, parts[index], context)
                provider_ids.append(result.provider_message_id or "")
                sent = index + 1
                if len(parts) > 1 and sent < len(parts):
                    await self._work.record_part(work.message_id, sent, provider_ids)
        except DeliveryError as exc:
            if sent > work.parts_sent:
                await self._work.record_part(work.message_id, sent, provider_ids)
            await self._failed(job, work, exc, breaker)
            return
        if breaker is not None:
            breaker.record_success()
        first_id = next((pid for pid in provider_ids if pid), None)
        await self._work.finish_outbound(work.message_id, OutboundStatus.SENT, provider_message_id=first_id)
        DELIVERIES.labels(work.channel.value, "sent").inc()
        if work.inbound_created_at is not None:
            END_TO_END_SECONDS.labels(work.channel.value).observe((utcnow() - work.inbound_created_at).total_seconds())

    async def _wait_part(self, channel: Channel) -> bool:
        throttle = self._throttles.get(channel)
        if throttle is None:
            return True
        wait = await throttle.acquire()
        if wait == 0:
            return True
        if wait > _MAX_INLINE_WAIT:
            return False
        await asyncio.sleep(wait)
        return await throttle.acquire() == 0

    async def _failed(self, job: Job, work: OutboundWork, exc: DeliveryError, breaker: CircuitBreaker | None) -> None:
        channel = work.channel.value
        logger.warning(
            "delivery_failed",
            extra={"ctx": {"kind": exc.kind.value, "code": exc.code, "attempts": work.attempts, "detail": str(exc)}},
        )
        DELIVERIES.labels(channel, exc.kind.value).inc()
        if exc.kind in (FailureKind.PERMANENT, FailureKind.WINDOW_CLOSED):
            await self._work.finish_outbound(
                work.message_id, OutboundStatus.FAILED, error_code=f"{exc.kind.value}:{exc.code}", error_detail=str(exc)
            )
            return
        if breaker is not None:
            if exc.kind is FailureKind.AUTH:
                breaker.force_open()
            elif exc.kind is FailureKind.RETRYABLE:
                breaker.record_failure()
        if work.attempts >= self._config.max_attempts:
            await self._work.finish_outbound(
                work.message_id, OutboundStatus.DEAD, error_code=f"{exc.kind.value}:{exc.code}", error_detail=str(exc)
            )
            try:
                await self._queue.dead_letter(job, f"{exc.kind.value}:{exc.code}")
            except QueueUnavailable:
                logger.error("dead_letter_publish_failed")
            DELIVERIES.labels(channel, "dead").inc()
            return
        delay = self._backoff.delay(work.attempts)
        if exc.retry_after:
            delay = max(delay, exc.retry_after) if exc.kind is FailureKind.RATE_LIMITED else delay
        if exc.kind is FailureKind.AUTH and breaker is not None:
            delay = max(delay, breaker.retry_after())
        await self._defer(job, work, delay, count_attempt=True, reason=f"{exc.kind.value}:{exc.code}")

    async def _defer(self, job: Job, work: OutboundWork, delay: float, *, count_attempt: bool, reason: str) -> None:
        await self._work.release_outbound(work.message_id, count_attempt=count_attempt, error_code=reason[:80])
        try:
            await self._queue.publish(job.retried(), delay_seconds=delay)
        except QueueUnavailable:
            logger.warning("outbound_retry_deferred_to_sweeper")
