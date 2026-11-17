"""Worker de entrada: reclama el mensaje, decide la respuesta y la deja lista para enviar.

- Idempotente: si el mensaje ya se procesó (o lo tiene otro worker) el trabajo se descarta.
- Errores transitorios del motor (modelo saturado o caído): se libera el mensaje y se reprograma
  con backoff exponencial y jitter. Agotados los intentos, se responde con un aviso de servicio
  no disponible para que la persona no quede sin respuesta.
- Cualquier otra falla antes de guardar la respuesta (por ejemplo, PostgreSQL reiniciándose): se
  libera el mensaje y el error sigue a la cola, que reintenta con espera. Si la base sigue caída la
  liberación también falla y el mensaje vuelve cuando vence la reserva (sweeper).
- Mientras se genera la respuesta se muestra "escribiendo..." donde el canal lo permite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from opentelemetry.trace import SpanKind

from iamia.ai.prompts import UNAVAILABLE_MESSAGE
from iamia.channels.models import OutboundContent, Recipient
from iamia.channels.registry import ChannelRegistry
from iamia.conversations.manager import ConversationManager
from iamia.conversations.models import ConversationState, Decision, InboundStatus, InboundWork, OutboundDraft
from iamia.core.clock import utcnow
from iamia.core.errors import RetryableError
from iamia.core.logs import log_context
from iamia.core.metrics import MESSAGES_PROCESSED, PROCESSING_SECONDS
from iamia.core.resilience import Backoff
from iamia.core.tracing import root_span, set_attributes
from iamia.messaging.contracts import Job, JobQueue, QueueUnavailable
from iamia.persistence.work import WorkRepository
from iamia.services.publisher import JobPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundConfig:
    lease_seconds: int = 180
    max_attempts: int = 4
    max_age_seconds: int = 86_400
    typing_timeout_seconds: float = 5.0


class InboundProcessor:
    def __init__(
        self,
        *,
        work: WorkRepository,
        manager: ConversationManager,
        registry: ChannelRegistry,
        queue: JobQueue,
        publisher: JobPublisher,
        config: InboundConfig | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        self._work = work
        self._manager = manager
        self._registry = registry
        self._queue = queue
        self._publisher = publisher
        self._config = config or InboundConfig()
        self._backoff = backoff or Backoff(base=2.0, factor=3.0, max_delay=600.0)

    async def handle(self, job: Job) -> None:
        with (
            log_context(message_id=job.message_id, channel=job.channel, trace_id=job.trace_id),
            root_span(
                "inbound.process",
                job.trace_id,
                kind=SpanKind.CONSUMER,
                **{"iamia.channel": job.channel, "iamia.message_id": job.message_id, "iamia.retries": job.retries},
            ) as span,
        ):
            work = await self._work.claim_inbound(job.message_id, self._config.lease_seconds)
            if work is None:
                return  # ya procesado o en manos de otro worker
            started = time.perf_counter()
            try:
                decision = await self._decide(job, work)
                if decision is None:
                    return
                reply_ids = await self._work.finish_inbound(work, decision)
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await self._work.release_inbound(work.message_id, type(exc).__name__)
                raise
            if reply_ids is None:
                logger.warning("inbound_lease_lost")
                return
            channel = work.contact.channel.value
            set_attributes(span, **{"iamia.outcome": decision.outcome, "iamia.replies": len(reply_ids)})
            await self._publisher.publish([Job("outbound", rid, channel, job.trace_id) for rid in reply_ids])
            MESSAGES_PROCESSED.labels(channel, decision.outcome.split(":")[0]).inc()
            PROCESSING_SECONDS.labels(channel).observe(time.perf_counter() - started)

    async def _decide(self, job: Job, work: InboundWork) -> Decision | None:
        reference = work.sent_at or work.created_at
        if (utcnow() - reference).total_seconds() > self._config.max_age_seconds:
            return Decision(InboundStatus.IGNORED, "expired")
        typing = self._start_typing(work)
        try:
            return await self._manager.handle(work)
        except RetryableError as exc:
            if work.attempts >= self._config.max_attempts:
                logger.error("inbound_gave_up", extra={"ctx": {"error": type(exc).__name__, "attempts": work.attempts}})
                draft = OutboundDraft(OutboundContent(UNAVAILABLE_MESSAGE))
                return Decision(InboundStatus.ANSWERED, "unavailable", (draft,))
            delay = max(self._backoff.delay(work.attempts), exc.retry_after)
            await self._work.release_inbound(work.message_id, type(exc).__name__)
            try:
                await self._queue.publish(job.retried(), delay_seconds=delay)
            except QueueUnavailable:
                logger.warning("inbound_retry_deferred_to_sweeper")
            return None
        finally:
            if typing is not None:
                typing.cancel()

    def _start_typing(self, work: InboundWork) -> asyncio.Task[None] | None:
        adapter = self._registry.find(work.contact.channel.value)
        if (
            adapter is None
            or adapter.typing is None
            or not work.kind.has_text
            or work.contact.opted_out
            or work.conversation.state is not ConversationState.BOT
        ):
            return None
        recipient = Recipient(work.contact.channel, work.contact.address, work.contact.account_id)
        typing = adapter.typing

        async def notify() -> None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    typing.notify_processing(recipient, work.provider_message_id), self._config.typing_timeout_seconds
                )

        return asyncio.create_task(notify())
