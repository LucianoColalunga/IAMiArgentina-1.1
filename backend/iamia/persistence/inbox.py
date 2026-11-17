"""Entrada de mensajes: alta de contacto, conversación y mensaje en una sola transacción."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg

from iamia.channels.models import DeliveryState, IdentityChange, InboundMessage, StatusUpdate
from iamia.core.clock import utcnow
from iamia.persistence.codec import DataCodec

# Orden de los estados de entrega: nunca se retrocede (un "delivered" atrasado no pisa un "read").
_STATUS_RANK_SQL = """
    CASE {column} WHEN 'pending' THEN 0 WHEN 'sending' THEN 0 WHEN 'sent' THEN 1
                  WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END
"""


@dataclass(frozen=True, slots=True)
class StoredInbound:
    message_id: str
    conversation_id: str
    contact_id: str


class InboxRepository:
    def __init__(self, pool: asyncpg.Pool, codec: DataCodec, *, idle_minutes: int) -> None:
        self._pool = pool
        self._codec = codec
        self._idle_minutes = idle_minutes

    async def store(self, message: InboundMessage) -> StoredInbound | None:
        """Guarda el mensaje. None si ya se había recibido (reintento de la plataforma)."""
        address_hash = self._codec.address_hash(message.channel, message.sender_id)
        address_enc = self._codec.encrypt_address(message.channel, address_hash, message.sender_id)
        message_id = str(uuid.uuid4())
        body_enc = self._codec.encrypt_inbound(message_id, message.text)
        now = utcnow()
        sent_at = min(message.sent_at, now)
        meta = {
            "attachments": [
                {"kind": item.kind.value, "mime": item.mime_type, "size": item.size_bytes}
                for item in message.attachments
            ],
        }
        if message.extra.get("payload"):
            meta["payload"] = message.extra["payload"]
        if message.reply_to:
            meta["reply_to_provider_id"] = message.reply_to

        async with self._pool.acquire() as conn, conn.transaction():
            contact = await conn.fetchrow(
                """
                INSERT INTO contacts (id, channel, account_id, address_hash, address_enc, locale, last_inbound_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (channel, address_hash) DO UPDATE
                   SET last_seen_at = now(),
                       -- Cifrada otra vez con la clave activa: los contactos que escriben quedan
                       -- al día solos después de una rotación.
                       address_enc = EXCLUDED.address_enc,
                       account_id = EXCLUDED.account_id,
                       locale = COALESCE(EXCLUDED.locale, contacts.locale),
                       last_inbound_at = GREATEST(contacts.last_inbound_at, EXCLUDED.last_inbound_at)
                RETURNING id
                """,
                uuid.uuid4(),
                message.channel.value,
                message.account_id,
                address_hash,
                address_enc,
                message.locale,
                sent_at,
            )
            contact_id = contact["id"]
            # Una conversación del bot sin actividad se cierra; las derivadas a personas siguen abiertas.
            await conn.execute(
                """
                UPDATE conversations SET state = 'closed', closed_at = now(), close_reason = 'idle'
                 WHERE contact_id = $1 AND state = 'bot'
                   AND last_activity_at < now() - make_interval(mins => $2)
                """,
                contact_id,
                self._idle_minutes,
            )
            conversation = await conn.fetchrow(
                """
                INSERT INTO conversations (id, contact_id, channel, thread_ref, subject)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (contact_id) WHERE state <> 'closed' DO UPDATE
                   SET last_activity_at = now(),
                       -- Hilo y asunto van juntos: la respuesta contesta el último correo con su
                       -- asunto (si la persona escribe otro correo con otro asunto, no el primero).
                       thread_ref = COALESCE(EXCLUDED.thread_ref, conversations.thread_ref),
                       subject = COALESCE(EXCLUDED.subject, conversations.subject)
                RETURNING id
                """,
                uuid.uuid4(),
                contact_id,
                message.channel.value,
                message.thread_ref,
                message.subject,
            )
            inserted = await conn.fetchval(
                """
                INSERT INTO messages (id, conversation_id, contact_id, channel, direction, author, kind,
                                      body_enc, provider_message_id, status, sent_at, meta)
                VALUES ($1, $2, $3, $4, 'in', 'user', $5, $6, $7, 'received', $8, $9)
                ON CONFLICT (channel, provider_message_id) WHERE direction = 'in' DO NOTHING
                RETURNING id
                """,
                uuid.UUID(message_id),
                conversation["id"],
                contact_id,
                message.channel.value,
                message.kind.value,
                body_enc,
                message.provider_message_id,
                sent_at,
                meta,
            )
        if inserted is None:
            return None
        return StoredInbound(message_id, str(conversation["id"]), str(contact_id))

    async def mark_enqueued(self, message_ids: list[str]) -> None:
        if message_ids:
            await self._pool.execute(
                "UPDATE messages SET enqueued_at = now() WHERE id = ANY($1::uuid[])",
                [uuid.UUID(i) for i in message_ids],
            )

    async def apply_status(self, update: StatusUpdate) -> bool:
        new_rank = _STATUS_RANK_SQL.format(column="$3::text")
        current_rank = _STATUS_RANK_SQL.format(column="status")
        state = "failed" if update.state is DeliveryState.FAILED else update.state.value
        result = await self._pool.execute(
            f"""
            UPDATE messages
               SET status = $3, updated_at = now(),
                   error_code = COALESCE($4, error_code), error_detail = COALESCE($5, error_detail)
             WHERE channel = $1 AND provider_message_id = $2 AND direction = 'out'
               AND status NOT IN ('dead') AND ({current_rank}) < ({new_rank})
            """,  # noqa: S608 - solo se interpolan expresiones fijas de este módulo
            update.channel.value,
            update.provider_message_id,
            state,
            update.error_code,
            update.error_title,
        )
        return result.endswith(" 1")

    async def apply_identity_change(self, change: IdentityChange) -> bool:
        """Re-asocia el contacto al identificador nuevo (si no existe ya otro contacto con ese id)."""
        old_hash = self._codec.address_hash(change.channel, change.previous_sender_id)
        new_hash = self._codec.address_hash(change.channel, change.new_sender_id)
        new_enc = self._codec.encrypt_address(change.channel, new_hash, change.new_sender_id)
        result = await self._pool.execute(
            """
            UPDATE contacts SET address_hash = $3, address_enc = $4, last_seen_at = now()
             WHERE channel = $1 AND address_hash = $2
               AND NOT EXISTS (SELECT 1 FROM contacts WHERE channel = $1 AND address_hash = $3)
            """,
            change.channel.value,
            old_hash,
            new_hash,
            new_enc,
        )
        return result.endswith(" 1")

    async def ping(self) -> bool:
        try:
            return await self._pool.fetchval("SELECT 1") == 1
        except (OSError, asyncpg.PostgresError):
            return False
