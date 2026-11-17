"""Modelo de mensaje normalizado, común a todos los canales."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType


class Channel(StrEnum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    MESSENGER = "messenger"
    X = "x"
    WEBCHAT = "webchat"
    EMAIL = "email"
    SMS = "sms"

    @classmethod
    def parse(cls, value: str) -> Channel:
        return cls(value.strip().lower())


class MessageKind(StrEnum):
    TEXT = "text"
    INTERACTIVE = "interactive"  # respuesta a un botón o lista
    DOCUMENT = "document"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    LOCATION = "location"
    STICKER = "sticker"
    REACTION = "reaction"
    CONTACT = "contact"
    UNSUPPORTED = "unsupported"

    @property
    def has_text(self) -> bool:
        return self in (MessageKind.TEXT, MessageKind.INTERACTIVE)


_EMPTY: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Attachment:
    """Metadatos de un adjunto. El contenido nunca se descarga por defecto (minimización)."""

    kind: MessageKind
    mime_type: str | None = None
    size_bytes: int | None = None
    file_name: str | None = None
    provider_ref: str | None = None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Mensaje recibido de una plataforma, ya traducido al formato común."""

    channel: Channel
    provider_message_id: str  # id del mensaje en la plataforma (clave de idempotencia)
    sender_id: str  # identificador del usuario en la plataforma (teléfono, chat id, PSID, ...)
    account_id: str  # cuenta propia que recibió el mensaje (número, página, bot, ...)
    kind: MessageKind
    sent_at: datetime
    text: str | None = None
    attachments: tuple[Attachment, ...] = ()
    sender_name: str | None = None
    locale: str | None = None
    reply_to: str | None = None  # id en la plataforma del mensaje citado
    subject: str | None = None  # correo
    thread_ref: str | None = None  # hilo o conversación de la plataforma (correo, X)
    extra: Mapping[str, str] = field(default=_EMPTY)


class DeliveryState(StrEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """Estado de entrega de un mensaje que enviamos, informado por la plataforma."""

    channel: Channel
    provider_message_id: str
    state: DeliveryState
    at: datetime
    error_code: str | None = None
    error_title: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityChange:
    """La plataforma avisa que el identificador de un usuario cambió (WhatsApp regenera el BSUID
    cuando la persona cambia de número). El contacto se re-asocia sin perder su conversación."""

    channel: Channel
    previous_sender_id: str
    new_sender_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class WebhookBatch:
    """Resultado de interpretar un webhook: puede traer varios mensajes, estados y avisos."""

    messages: tuple[InboundMessage, ...] = ()
    statuses: tuple[StatusUpdate, ...] = ()
    ignored: int = 0  # eventos válidos que no nos interesan (ecos, grupos, lecturas, ...)
    identity_changes: tuple[IdentityChange, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceLink:
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class OutboundContent:
    """Lo que el núcleo quiere decir, independiente del canal."""

    text: str
    sources: tuple[SourceLink, ...] = ()


@dataclass(frozen=True, slots=True)
class RenderedPart:
    """Una parte lista para enviar por un canal concreto (respeta su largo máximo y formato)."""

    text: str


@dataclass(frozen=True, slots=True)
class Recipient:
    """Destinatario de una respuesta, con lo necesario para aplicar las reglas del canal."""

    channel: Channel
    address: str
    account_id: str
    last_inbound_at: datetime | None = None
    thread_ref: str | None = None
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class SendContext:
    now: datetime
    operator_reply: bool = False  # escrito por una persona (habilita etiquetas como HUMAN_AGENT)
    message_id: str = ""  # id interno del mensaje saliente (trazabilidad)


@dataclass(frozen=True, slots=True)
class SendResult:
    provider_message_id: str | None


class FailureKind(StrEnum):
    RETRYABLE = "retryable"  # error transitorio de la plataforma o de red
    RATE_LIMITED = "rate_limited"  # la plataforma pidió bajar el ritmo
    WINDOW_CLOSED = "window_closed"  # fuera de la ventana de atención del canal (24 h en Meta)
    PERMANENT = "permanent"  # el mensaje no se va a poder entregar (usuario bloqueó, inválido)
    AUTH = "auth"  # credenciales vencidas o sin permisos: afecta a todo el canal


class DeliveryError(Exception):
    def __init__(
        self, kind: FailureKind, detail: str, *, code: str | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.code = code
        self.retry_after = retry_after

    @property
    def counts_against_channel(self) -> bool:
        """Errores que indican que el canal (no el destinatario) está con problemas."""
        return self.kind in (FailureKind.RETRYABLE, FailureKind.AUTH)
