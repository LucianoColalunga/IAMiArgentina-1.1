"""Constructores de objetos de dominio para las pruebas (valores por defecto razonables)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from iamia.channels.models import Channel, InboundMessage, MessageKind, OutboundContent, Recipient
from iamia.conversations.models import (
    Author,
    ContactInfo,
    ConversationInfo,
    ConversationState,
    InboundWork,
    OutboundWork,
)
from iamia.core.clock import utcnow


def contact(**overrides: Any) -> ContactInfo:
    base = ContactInfo(
        id="c0ffee00-0000-4000-8000-000000000001",
        channel=Channel.TELEGRAM,
        account_id="bot-1",
        address="5551234",
        address_hash="a" * 64,
        last_inbound_at=utcnow(),
    )
    return replace(base, **overrides)


def conversation(**overrides: Any) -> ConversationInfo:
    return replace(
        ConversationInfo(id="c0ffee00-0000-4000-8000-0000000000aa", state=ConversationState.BOT), **overrides
    )


def inbound_work(text: str | None = "¿Cómo saco el DNI digital?", **overrides: Any) -> InboundWork:
    now = utcnow()
    base = InboundWork(
        message_id="c0ffee00-0000-4000-8000-0000000000bb",
        contact=contact(),
        conversation=conversation(),
        kind=MessageKind.TEXT,
        text=text,
        provider_message_id="tg-1",
        sent_at=now,
        created_at=now,
        attempts=1,
    )
    return replace(base, **overrides)


def outbound_work(text: str = "Respuesta de prueba", **overrides: Any) -> OutboundWork:
    now = utcnow()
    base = OutboundWork(
        message_id="c0ffee00-0000-4000-8000-0000000000cc",
        channel=Channel.TELEGRAM,
        author=Author.BOT,
        content=OutboundContent(text),
        recipient=Recipient(Channel.TELEGRAM, "5551234", "bot-1", last_inbound_at=now),
        attempts=1,
        created_at=now,
        inbound_created_at=now - timedelta(seconds=2),
    )
    return replace(base, **overrides)


def inbound_message(
    provider_id: str = "m-1", *, channel: Channel = Channel.TELEGRAM, sent_at: datetime | None = None, **extra: Any
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        provider_message_id=provider_id,
        sender_id=extra.pop("sender_id", "5551234"),
        account_id=extra.pop("account_id", "bot-1"),
        kind=extra.pop("kind", MessageKind.TEXT),
        sent_at=sent_at or utcnow(),
        # Una consulta (no un saludo: "hola" solo recibe la bienvenida sin pasar por el motor).
        text=extra.pop("text", "¿Cómo saco el DNI digital?"),
        **extra,
    )
