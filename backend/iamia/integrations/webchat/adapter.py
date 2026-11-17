"""Chat web.

No hay plataforma externa: los mensajes entran por la API propia (/v1/webchat) y las
respuestas quedan guardadas en la base. "Enviar" una respuesta es avisarle a la réplica de la
API que tiene al navegador esperando (long polling) mediante Redis Pub/Sub.

Cada proceso de la API escucha un único patrón (iamia:webchat:*) y despierta solo a los
navegadores conectados a él; así no se abre una conexión a Redis por cada cliente esperando.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities
from iamia.channels.formatting import PlainTextFormatter
from iamia.channels.models import (
    Channel,
    DeliveryError,
    FailureKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
)

logger = logging.getLogger(__name__)

CAPABILITIES = ChannelCapabilities(max_text_chars=8000)
_CHANNEL_PREFIX = "iamia:webchat:"


def notification_channel(session_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{session_id}"


class WebchatPublisher:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, session_id: str) -> None:
        await self._redis.publish(notification_channel(session_id), "1")


class WebchatSender:
    """La respuesta ya está en la base; el envío solo notifica al navegador."""

    def __init__(self, publisher: WebchatPublisher) -> None:
        self._publisher = publisher

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        try:
            await self._publisher.publish(recipient.address)
        except RedisError as exc:
            raise DeliveryError(FailureKind.RETRYABLE, "no se pudo notificar al chat web", code="redis") from exc
        return SendResult(context.message_id or None)


class WebchatHub:
    """Despierta a los pedidos de long polling de este proceso cuando llega una respuesta."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._listen(), name="webchat-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait(self, session_id: str, max_wait: float) -> bool:
        """Espera una notificación para la sesión. Devuelve False si venció el tiempo."""
        event = asyncio.Event()
        self._waiters.setdefault(session_id, set()).add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout=max_wait)
            return True
        except TimeoutError:
            return False
        finally:
            waiters = self._waiters.get(session_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._waiters.pop(session_id, None)

    @property
    def waiting(self) -> int:
        return sum(len(events) for events in self._waiters.values())

    def _wake(self, session_id: str) -> None:
        for event in self._waiters.get(session_id, ()):
            event.set()

    async def _listen(self) -> None:
        while True:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.psubscribe(f"{_CHANNEL_PREFIX}*")
                while True:
                    message = await pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "pmessage":
                        channel = message.get("channel")
                        if isinstance(channel, bytes):
                            channel = channel.decode("utf-8", "ignore")
                        self._wake(str(channel).removeprefix(_CHANNEL_PREFIX))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("webchat_hub_reconnecting", extra={"ctx": {"error": type(exc).__name__}})
                await asyncio.sleep(1.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()


def build_webchat_adapter(redis: Redis) -> ChannelAdapter:
    return ChannelAdapter(
        channel=Channel.WEBCHAT,
        capabilities=CAPABILITIES,
        formatter=PlainTextFormatter(CAPABILITIES.max_text_chars, include_sources=False),
        sender=WebchatSender(WebchatPublisher(redis)),
    )
