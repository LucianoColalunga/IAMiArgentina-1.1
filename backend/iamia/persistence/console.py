"""Consultas y acciones de la consola de operadores.

Los operadores ven direcciones parcialmente ocultas (****1234) y los mensajes con los datos
personales ya enmascarados. Cada acción queda en la auditoría (la registra el servicio).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncpg

from iamia.channels.models import Channel
from iamia.conversations.models import Author
from iamia.core.text import truncate
from iamia.persistence.codec import DataCodec
from iamia.security.pii import mask_address


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# La cola de atención va por orden de llegada: primero la que más espera. Las conversaciones con
# el asistente y las cerradas son miles: de la más reciente a la más vieja. Las dos consultas
# recorren el índice (state, last_activity_at) sin ordenar la tabla.
_LIST_OLDEST_FIRST = """
    SELECT v.id, v.channel, v.state, v.assigned_to, v.handoff_reason, v.opened_at, v.last_activity_at,
           c.address_hash, c.address_enc,
           m.id AS last_id, m.author AS last_author, m.body_enc AS last_body
      FROM conversations v
      JOIN contacts c ON c.id = v.contact_id
      LEFT JOIN LATERAL (
           SELECT id, author, body_enc FROM messages
            WHERE conversation_id = v.id AND body_enc IS NOT NULL
            ORDER BY seq DESC LIMIT 1) m ON true
     WHERE v.state = ANY($1::text[])
     ORDER BY v.last_activity_at ASC
     LIMIT $2
"""
_LIST_NEWEST_FIRST = """
    SELECT v.id, v.channel, v.state, v.assigned_to, v.handoff_reason, v.opened_at, v.last_activity_at,
           c.address_hash, c.address_enc,
           m.id AS last_id, m.author AS last_author, m.body_enc AS last_body
      FROM conversations v
      JOIN contacts c ON c.id = v.contact_id
      LEFT JOIN LATERAL (
           SELECT id, author, body_enc FROM messages
            WHERE conversation_id = v.id AND body_enc IS NOT NULL
            ORDER BY seq DESC LIMIT 1) m ON true
     WHERE v.state = ANY($1::text[])
     ORDER BY v.last_activity_at DESC
     LIMIT $2
"""
_QUEUE_STATES = frozenset({"waiting_human", "human"})


class ConsoleRepository:
    def __init__(self, pool: asyncpg.Pool, codec: DataCodec) -> None:
        self._pool = pool
        self._codec = codec

    def _masked(self, row: asyncpg.Record) -> str:
        try:
            return mask_address(self._codec.decrypt_address(row["channel"], row["address_hash"], row["address_enc"]))
        except ValueError:
            return "****"

    def _text(self, row: asyncpg.Record) -> str:
        message_id = str(row["id"])
        if row["author"] == Author.USER.value:
            return self._codec.decrypt_text(message_id, row["body_enc"]) or ""
        return self._codec.decrypt_content(message_id, row["body_enc"]).text

    async def list_conversations(self, states: list[str], *, limit: int = 50) -> list[dict[str, Any]]:
        query = _LIST_OLDEST_FIRST if set(states) <= _QUEUE_STATES else _LIST_NEWEST_FIRST
        rows = await self._pool.fetch(query, states, min(max(limit, 1), 200))
        result = []
        for row in rows:
            preview = ""
            if row["last_id"] is not None:
                preview = self._text({"id": row["last_id"], "author": row["last_author"], "body_enc": row["last_body"]})
            result.append(
                {
                    "id": str(row["id"]),
                    "channel": row["channel"],
                    "state": row["state"],
                    "assigned_to": row["assigned_to"],
                    "handoff_reason": row["handoff_reason"],
                    "opened_at": _iso(row["opened_at"]),
                    "last_activity_at": _iso(row["last_activity_at"]),
                    "contact": self._masked(row),
                    "preview": truncate(preview, 140),
                }
            )
        return result

    async def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conv = await self._pool.fetchrow(
            """
            SELECT v.id, v.channel, v.state, v.assigned_to, v.handoff_reason, v.opened_at, v.last_activity_at,
                   v.no_info_count, c.address_hash, c.address_enc, c.opted_out, c.person_id
              FROM conversations v JOIN contacts c ON c.id = v.contact_id
             WHERE v.id = $1
            """,
            uuid.UUID(conversation_id),
        )
        if conv is None:
            return None
        rows = await self._pool.fetch(
            """
            SELECT id, seq, direction, author, kind, body_enc, status, outcome, operator, error_code, created_at
              FROM messages WHERE conversation_id = $1 ORDER BY seq DESC LIMIT 200
            """,
            uuid.UUID(conversation_id),
        )
        messages = [
            {
                "id": str(row["id"]),
                "seq": row["seq"],
                "direction": row["direction"],
                "author": row["author"],
                "kind": row["kind"],
                "text": self._text(row) if row["body_enc"] else "",
                "status": row["status"],
                "outcome": row["outcome"],
                "operator": row["operator"],
                "error_code": row["error_code"],
                "created_at": _iso(row["created_at"]),
            }
            for row in reversed(rows)
        ]
        return {
            "id": str(conv["id"]),
            "channel": conv["channel"],
            "state": conv["state"],
            "assigned_to": conv["assigned_to"],
            "handoff_reason": conv["handoff_reason"],
            "no_info_count": conv["no_info_count"],
            "opened_at": _iso(conv["opened_at"]),
            "last_activity_at": _iso(conv["last_activity_at"]),
            "contact": self._masked(conv),
            "opted_out": conv["opted_out"],
            "linked": conv["person_id"] is not None,
            "messages": messages,
        }

    async def claim(self, conversation_id: str, operator: str, *, can_reassign: bool) -> bool:
        result = await self._pool.execute(
            """
            UPDATE conversations SET state = 'human', assigned_to = $2, last_activity_at = now()
             WHERE id = $1 AND state IN ('waiting_human', 'human', 'bot')
               AND (assigned_to IS NULL OR assigned_to = $2 OR $3)
            """,
            uuid.UUID(conversation_id),
            operator,
            can_reassign,
        )
        return result.endswith(" 1")

    async def set_state(self, conversation_id: str, state: str, *, operator: str, reason: str) -> bool:
        result = await self._pool.execute(
            """
            UPDATE conversations
               SET state = $2, assigned_to = NULL, last_activity_at = now(),
                   closed_at = CASE WHEN $2 = 'closed' THEN now() ELSE NULL END,
                   close_reason = CASE WHEN $2 = 'closed' THEN $4 ELSE NULL END,
                   no_info_count = 0
             WHERE id = $1 AND state <> 'closed' AND (assigned_to IS NULL OR assigned_to = $3 OR $3 = '*')
            """,
            uuid.UUID(conversation_id),
            state,
            operator,
            reason,
        )
        return result.endswith(" 1")

    async def assigned_to(self, conversation_id: str) -> tuple[str, str | None] | None:
        row = await self._pool.fetchrow(
            "SELECT state, assigned_to FROM conversations WHERE id = $1", uuid.UUID(conversation_id)
        )
        return (row["state"], row["assigned_to"]) if row else None

    async def problems(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT id, conversation_id, channel, author, status, attempts, error_code, error_detail, updated_at
              FROM messages WHERE direction = 'out' AND status IN ('failed', 'dead')
             ORDER BY updated_at DESC LIMIT $1
            """,
            min(max(limit, 1), 500),
        )
        return [
            {
                "id": str(row["id"]),
                "conversation_id": str(row["conversation_id"]),
                "channel": row["channel"],
                "author": row["author"],
                "status": row["status"],
                "attempts": row["attempts"],
                "error_code": row["error_code"],
                "error_detail": row["error_detail"],
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ]

    async def retry_message(self, message_id: str) -> Channel | None:
        channel = await self._pool.fetchval(
            """
            UPDATE messages SET status = 'pending', attempts = 0, error_code = NULL, error_detail = NULL,
                   enqueued_at = now(), updated_at = now()
             WHERE id = $1 AND direction = 'out' AND status IN ('failed', 'dead')
            RETURNING channel
            """,
            uuid.UUID(message_id),
        )
        return Channel.parse(channel) if channel else None

    async def channel_summary(self) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT channel, direction, status, count(*) AS total
              FROM messages WHERE created_at > now() - interval '24 hours'
             GROUP BY channel, direction, status ORDER BY channel, direction, status
            """
        )
        return [dict(row) for row in rows]

    async def erase_contact_of(self, conversation_id: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM contacts WHERE id = (SELECT contact_id FROM conversations WHERE id = $1)",
            uuid.UUID(conversation_id),
        )
        return result.endswith(" 1")
