"""Contratos que implementa cada integración.

Están separados por responsabilidad (Interface Segregation): un canal sin webhooks (chat web)
no implementa verificación, y uno sin indicador de "escribiendo" no implementa TypingNotifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol, runtime_checkable

from iamia.channels.models import (
    Channel,
    OutboundContent,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    WebhookBatch,
)


@dataclass(frozen=True)
class ChannelCapabilities:
    max_text_chars: int
    reply_window: timedelta | None = None  # ventana para respuestas libres (Meta: 24 h)
    human_agent_window: timedelta | None = None  # ventana extendida para operadores (HUMAN_AGENT: 7 días)
    supports_typing: bool = False
    markup: Literal["plain", "whatsapp"] = "plain"


@dataclass(frozen=True)
class WebhookRequest:
    """Lo que un adaptador necesita de un pedido HTTP, sin depender del framework web."""

    method: str
    url: str  # URL pública reconstruida (algunas firmas la incluyen, como la de Twilio)
    headers: Mapping[str, str]  # nombres en minúsculas
    query: Mapping[str, str]
    body: bytes
    received_at: float = 0.0  # epoch, para validar frescura


@dataclass(frozen=True)
class HttpReply:
    body: str
    media_type: str = "text/plain"
    status_code: int = 200


class WebhookVerifier(Protocol):
    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        """Responde el desafío de suscripción (GET). None = desafío inválido."""
        ...

    def authenticate(self, request: WebhookRequest) -> None:
        """Valida firma o secreto. Lanza WebhookAuthError si no corresponde."""
        ...


class WebhookParser(Protocol):
    def parse(self, request: WebhookRequest) -> WebhookBatch:
        """Traduce el cuerpo al formato común. Lanza WebhookPayloadError si es inválido."""
        ...


class MessageFormatter(Protocol):
    def render(self, content: OutboundContent) -> list[RenderedPart]: ...


class MessageSender(Protocol):
    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        """Envía una parte. Lanza DeliveryError clasificando la falla."""
        ...


class TypingNotifier(Protocol):
    async def notify_processing(self, recipient: Recipient, provider_message_id: str) -> None:
        """Marca el mensaje como leído y/o muestra "escribiendo..." mientras se genera la respuesta."""
        ...


@runtime_checkable
class AsyncClosable(Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class ChannelAdapter:
    """Todo lo que el núcleo sabe de un canal."""

    channel: Channel
    capabilities: ChannelCapabilities
    formatter: MessageFormatter
    sender: MessageSender
    verifier: WebhookVerifier | None = None
    parser: WebhookParser | None = None
    typing: TypingNotifier | None = None
    webhook_reply: HttpReply | None = None  # respuesta al POST del webhook (Twilio espera TwiML)

    @property
    def accepts_webhooks(self) -> bool:
        return self.verifier is not None and self.parser is not None
