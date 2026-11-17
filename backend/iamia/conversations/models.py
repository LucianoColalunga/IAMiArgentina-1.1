"""Modelo de dominio de las conversaciones (independiente de la base de datos y de los canales)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from iamia.ai.engine import Answer
from iamia.channels.models import Channel, MessageKind, OutboundContent, Recipient


class ConversationState(StrEnum):
    BOT = "bot"  # responde el asistente
    WAITING_HUMAN = "waiting_human"  # pidió una persona: el bot no responde hasta que alguien la tome
    HUMAN = "human"  # la atiende un operador
    CLOSED = "closed"


class Author(StrEnum):
    USER = "user"
    BOT = "bot"
    OPERATOR = "operator"
    SYSTEM = "system"


class InboundStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    ANSWERED = "answered"
    IGNORED = "ignored"
    FAILED = "failed"


class OutboundStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"  # no se puede entregar (bloqueo, ventana cerrada, destinatario inválido)
    DEAD = "dead"  # se agotaron los reintentos: queda para revisión manual


@dataclass(frozen=True, slots=True)
class Turn:
    author: Author
    text: str


@dataclass(frozen=True, slots=True)
class ContactInfo:
    id: str
    channel: Channel
    account_id: str
    address: str
    address_hash: str
    opted_out: bool = False
    person_id: str | None = None
    last_inbound_at: datetime | None = None
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationInfo:
    id: str
    state: ConversationState
    no_info_count: int = 0
    assigned_to: str | None = None
    thread_ref: str | None = None
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class InboundWork:
    """Un mensaje entrante reclamado por un worker, con todo lo necesario para decidir."""

    message_id: str
    contact: ContactInfo
    conversation: ConversationInfo
    kind: MessageKind
    text: str | None
    provider_message_id: str
    sent_at: datetime | None
    created_at: datetime
    attempts: int
    history: tuple[Turn, ...] = ()


@dataclass(frozen=True, slots=True)
class OutboundDraft:
    content: OutboundContent
    author: Author = Author.BOT
    operator: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """Qué hacer con un mensaje entrante. El worker lo persiste en una sola transacción."""

    status: InboundStatus
    outcome: str
    replies: tuple[OutboundDraft, ...] = ()
    state: ConversationState | None = None
    no_info_count: int | None = None
    handoff_reason: str | None = None
    answer: Answer | None = None


@dataclass(frozen=True, slots=True)
class OutboundWork:
    """Un mensaje saliente reclamado por el dispatcher."""

    message_id: str
    channel: Channel
    author: Author
    content: OutboundContent
    recipient: Recipient
    attempts: int
    created_at: datetime
    parts_sent: int = 0
    provider_ids: tuple[str, ...] = field(default=())
    inbound_created_at: datetime | None = None
