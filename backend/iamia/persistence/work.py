"""Procesamiento de mensajes con lease: reclamar, completar, liberar o descartar.

Un worker "reclama" un mensaje marcándolo en proceso con un vencimiento (lease). Si el worker
muere, el lease vence y el mensaje se puede volver a reclamar. Completar exige seguir siendo el
dueño (status = processing/sending): un worker lento no pisa a otro que ya lo tomó.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from iamia.channels.models import Channel, MessageKind, Recipient
from iamia.conversations.models import (
    Author,
    ContactInfo,
    ConversationInfo,
    ConversationState,
    Decision,
    InboundStatus,
    InboundWork,
    OutboundDraft,
    OutboundStatus,
    OutboundWork,
    Turn,
)
from iamia.persistence.codec import DataCodec


class WorkRepository:
    def __init__(self, pool: asyncpg.Pool, codec: DataCodec, *, context_turns: int = 3) -> None:
        self._pool = pool
        self._codec = codec
        self._context_turns = context_turns

    # ================================================================== entrada
    async def claim_inbound(self, message_id: str, lease_seconds: int) -> InboundWork | None:
        # Reserva y lecturas en una transacción: si la conexión se corta después de reservar (por
        # ejemplo, PostgreSQL reiniciándose), la reserva se deshace y el reintento lo toma enseguida
        # en vez de esperar a que venza.
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE messages m
                   SET status = 'processing', attempts = attempts + 1, updated_at = now(),
                       lease_until = now() + make_interval(secs => $2)
                 WHERE id = $1 AND direction = 'in'
                   AND (status = 'received' OR (status = 'processing' AND lease_until < now()))
                RETURNING id, conversation_id, contact_id, kind, body_enc, provider_message_id,
                          sent_at, created_at, attempts, seq
                """,
                uuid.UUID(message_id),
                float(lease_seconds),
            )
            if row is None:
                return None
            context = await conn.fetchrow(
                """
                SELECT c.channel, c.account_id, c.address_hash, c.address_enc, c.opted_out, c.person_id,
                       c.last_inbound_at, c.locale,
                       v.state, v.no_info_count, v.assigned_to, v.thread_ref, v.subject
                  FROM contacts c JOIN conversations v ON v.id = $2
                 WHERE c.id = $1
                """,
                row["contact_id"],
                row["conversation_id"],
            )
            history_rows = await conn.fetch(
                """
                SELECT id, author, body_enc FROM messages
                 WHERE conversation_id = $1 AND seq < $2 AND body_enc IS NOT NULL
                   AND author IN ('user', 'bot', 'operator')
                 ORDER BY seq DESC LIMIT $3
                """,
                row["conversation_id"],
                row["seq"],
                self._context_turns * 2,
            )
        if context is None:  # el contacto se borró mientras el mensaje esperaba
            await self._pool.execute(
                "UPDATE messages SET status = 'ignored', lease_until = NULL WHERE id = $1", row["id"]
            )
            return None
        channel = Channel.parse(context["channel"])
        contact = ContactInfo(
            id=str(row["contact_id"]),
            channel=channel,
            account_id=context["account_id"],
            address=self._codec.decrypt_address(channel, context["address_hash"], context["address_enc"]),
            address_hash=context["address_hash"],
            opted_out=context["opted_out"],
            person_id=str(context["person_id"]) if context["person_id"] else None,
            last_inbound_at=context["last_inbound_at"],
            locale=context["locale"],
        )
        conversation = ConversationInfo(
            id=str(row["conversation_id"]),
            state=ConversationState(context["state"]),
            no_info_count=context["no_info_count"],
            assigned_to=context["assigned_to"],
            thread_ref=context["thread_ref"],
            subject=context["subject"],
        )
        history = tuple(Turn(Author(item["author"]), self._history_text(item)) for item in reversed(history_rows))
        return InboundWork(
            message_id=str(row["id"]),
            contact=contact,
            conversation=conversation,
            kind=MessageKind(row["kind"]) if row["kind"] in MessageKind._value2member_map_ else MessageKind.UNSUPPORTED,
            text=self._codec.decrypt_text(str(row["id"]), row["body_enc"]),
            provider_message_id=row["provider_message_id"] or "",
            sent_at=row["sent_at"],
            created_at=row["created_at"],
            attempts=row["attempts"],
            history=tuple(turn for turn in history if turn.text),
        )

    def _history_text(self, row: asyncpg.Record) -> str:
        message_id = str(row["id"])
        if row["author"] == Author.USER.value:
            return self._codec.decrypt_text(message_id, row["body_enc"]) or ""
        return self._codec.decrypt_content(message_id, row["body_enc"]).text

    async def finish_inbound(self, work: InboundWork, decision: Decision) -> list[str] | None:
        """Guarda la decisión y las respuestas. None si otro worker ya se quedó con el mensaje."""
        answer = decision.answer
        reply_ids: list[str] = []
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.execute(
                """
                UPDATE messages
                   SET status = $2, outcome = $3, cache_layer = $4, kb_version = $5, latency_ms = $6,
                       lease_until = NULL, error_code = NULL, updated_at = now()
                 WHERE id = $1 AND status = 'processing'
                """,
                uuid.UUID(work.message_id),
                decision.status.value,
                decision.outcome,
                answer.cache_layer if answer else None,
                answer.kb_version if answer else None,
                answer.latency_ms if answer else None,
            )
            if not updated.endswith(" 1"):
                return None
            # Bloquear la conversación antes de insertar: así las respuestas de transacciones
            # concurrentes (otro mensaje, un operador) reciben su número de secuencia en el orden en
            # que se confirman y el chat web, que lee por secuencia, no saltea ninguna.
            await conn.execute("SELECT 1 FROM conversations WHERE id = $1 FOR UPDATE", uuid.UUID(work.conversation.id))
            for draft in decision.replies:
                reply_ids.append(
                    await self._insert_outbound(
                        conn,
                        work.conversation.id,
                        work.contact.id,
                        work.contact.channel,
                        draft,
                        reply_to=work.message_id,
                    )
                )
            await conn.execute(
                """
                UPDATE conversations
                   SET state = COALESCE($2, state),
                       no_info_count = COALESCE($3, no_info_count),
                       handoff_reason = COALESCE($4, handoff_reason),
                       handoff_at = CASE WHEN $2 = 'waiting_human' THEN now() ELSE handoff_at END,
                       closed_at = CASE WHEN $2 = 'closed' THEN now() ELSE closed_at END,
                       close_reason = CASE WHEN $2 = 'closed' THEN $5 ELSE close_reason END,
                       assigned_to = CASE WHEN $2 IN ('bot', 'closed') THEN NULL ELSE assigned_to END,
                       last_activity_at = now()
                 WHERE id = $1
                """,
                uuid.UUID(work.conversation.id),
                decision.state.value if decision.state else None,
                decision.no_info_count,
                decision.handoff_reason,
                decision.outcome,
            )
        return reply_ids

    async def _insert_outbound(
        self,
        conn: asyncpg.Connection,
        conversation_id: str,
        contact_id: str,
        channel: Channel,
        draft: OutboundDraft,
        *,
        reply_to: str | None,
    ) -> str:
        message_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO messages (id, conversation_id, contact_id, channel, direction, author, kind, body_enc,
                                  reply_to_id, status, operator)
            VALUES ($1, $2, $3, $4, 'out', $5, 'text', $6, $7, 'pending', $8)
            """,
            uuid.UUID(message_id),
            uuid.UUID(conversation_id),
            uuid.UUID(contact_id),
            channel.value,
            draft.author.value,
            self._codec.encrypt_content(message_id, draft.content),
            uuid.UUID(reply_to) if reply_to else None,
            draft.operator,
        )
        return message_id

    async def add_outbound(self, conversation_id: str, draft: OutboundDraft) -> tuple[str, Channel] | None:
        """Respuesta fuera del flujo de un entrante (operador, avisos del sistema)."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT contact_id, channel FROM conversations WHERE id = $1 FOR UPDATE", uuid.UUID(conversation_id)
            )
            if row is None:
                return None
            channel = Channel.parse(row["channel"])
            message_id = await self._insert_outbound(
                conn, conversation_id, str(row["contact_id"]), channel, draft, reply_to=None
            )
            await conn.execute(
                "UPDATE conversations SET last_activity_at = now() WHERE id = $1", uuid.UUID(conversation_id)
            )
        return message_id, channel

    async def release_inbound(self, message_id: str, error_code: str) -> None:
        await self._pool.execute(
            """
            UPDATE messages SET status = 'received', lease_until = NULL, enqueued_at = now(), error_code = $2,
                   updated_at = now()
             WHERE id = $1 AND status = 'processing'
            """,
            uuid.UUID(message_id),
            error_code[:80],
        )

    async def fail_inbound(self, message_id: str, error_code: str) -> None:
        await self._pool.execute(
            """
            UPDATE messages SET status = 'failed', lease_until = NULL, error_code = $2, updated_at = now()
             WHERE id = $1 AND status IN ('processing', 'received')
            """,
            uuid.UUID(message_id),
            error_code[:80],
        )

    # ================================================================== salida
    async def claim_outbound(self, message_id: str, lease_seconds: int) -> OutboundWork | None:
        async with self._pool.acquire() as conn, conn.transaction():  # igual que claim_inbound
            row = await conn.fetchrow(
                """
                UPDATE messages
                   SET status = 'sending', attempts = attempts + 1, updated_at = now(),
                       lease_until = now() + make_interval(secs => $2)
                 WHERE id = $1 AND direction = 'out'
                   AND (status = 'pending' OR (status = 'sending' AND lease_until < now()))
                RETURNING id, conversation_id, contact_id, channel, author, body_enc, attempts, created_at,
                          reply_to_id, meta
                """,
                uuid.UUID(message_id),
                float(lease_seconds),
            )
            if row is None:
                return None
            context = await conn.fetchrow(
                """
                SELECT c.account_id, c.address_hash, c.address_enc, c.last_inbound_at,
                       v.thread_ref, v.subject, r.created_at AS inbound_created_at
                  FROM contacts c
                  JOIN conversations v ON v.id = $2
                  LEFT JOIN messages r ON r.id = $3
                 WHERE c.id = $1
                """,
                row["contact_id"],
                row["conversation_id"],
                row["reply_to_id"],
            )
        if context is None:
            await self.finish_outbound(str(row["id"]), OutboundStatus.FAILED, error_code="contact_deleted")
            return None
        channel = Channel.parse(row["channel"])
        meta: dict[str, Any] = row["meta"] or {}
        recipient = Recipient(
            channel=channel,
            address=self._codec.decrypt_address(channel, context["address_hash"], context["address_enc"]),
            account_id=context["account_id"],
            last_inbound_at=context["last_inbound_at"],
            thread_ref=context["thread_ref"],
            subject=context["subject"],
        )
        return OutboundWork(
            message_id=str(row["id"]),
            channel=channel,
            author=Author(row["author"]),
            content=self._codec.decrypt_content(str(row["id"]), row["body_enc"]),
            recipient=recipient,
            attempts=row["attempts"],
            created_at=row["created_at"],
            parts_sent=int(meta.get("parts_sent", 0)),
            provider_ids=tuple(meta.get("provider_ids", [])),
            inbound_created_at=context["inbound_created_at"],
        )

    async def record_part(self, message_id: str, parts_sent: int, provider_ids: list[str]) -> None:
        await self._pool.execute(
            """
            UPDATE messages SET meta = meta || jsonb_build_object('parts_sent', $2::int, 'provider_ids', $3::jsonb),
                   updated_at = now()
             WHERE id = $1 AND status = 'sending'
            """,
            uuid.UUID(message_id),
            parts_sent,
            provider_ids,
        )

    async def finish_outbound(
        self,
        message_id: str,
        status: OutboundStatus,
        *,
        provider_message_id: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        result = await self._pool.execute(
            """
            UPDATE messages
               SET status = $2, provider_message_id = COALESCE($3, provider_message_id),
                   error_code = $4, error_detail = $5, lease_until = NULL, updated_at = now(),
                   sent_at = CASE WHEN $2 = 'sent' THEN now() ELSE sent_at END
             WHERE id = $1 AND status IN ('sending', 'pending')
            """,
            uuid.UUID(message_id),
            status.value,
            provider_message_id,
            error_code,
            (error_detail or "")[:300] or None,
        )
        return result.endswith(" 1")

    async def release_outbound(self, message_id: str, *, count_attempt: bool, error_code: str | None = None) -> None:
        await self._pool.execute(
            """
            UPDATE messages
               SET status = 'pending', lease_until = NULL, enqueued_at = now(), updated_at = now(),
                   attempts = attempts - CASE WHEN $2 THEN 0 ELSE 1 END,
                   error_code = COALESCE($3, error_code)
             WHERE id = $1 AND status = 'sending'
            """,
            uuid.UUID(message_id),
            count_attempt,
            error_code,
        )

    async def inbound_status(self, message_id: str) -> InboundStatus | None:
        value = await self._pool.fetchval("SELECT status FROM messages WHERE id = $1", uuid.UUID(message_id))
        return InboundStatus(value) if value in InboundStatus._value2member_map_ else None
