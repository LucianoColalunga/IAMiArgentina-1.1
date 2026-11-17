"""Consultas del worker de mantenimiento: republicación (sweeper), retención y borrados."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class PendingJob:
    message_id: str
    direction: str
    channel: str


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    messages_days: int = 90
    contacts_days: int = 180
    audit_days: int = 365


class MaintenanceRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def recover_expired_leases(self) -> int:
        """Mensajes de un worker que murió: vuelven a la fila para republicarse."""
        result = await self._pool.execute(
            """
            UPDATE messages
               SET status = CASE WHEN direction = 'in' THEN 'received' ELSE 'pending' END,
                   lease_until = NULL, enqueued_at = NULL, updated_at = now()
             WHERE status IN ('processing', 'sending') AND lease_until < now() - interval '30 seconds'
            """
        )
        return int(result.split()[-1])

    async def unpublished(
        self, *, older_than_seconds: int = 30, stale_after_seconds: int = 3600, limit: int = 1000
    ) -> list[PendingJob]:
        """Mensajes que nunca llegaron a la cola (cola caída o llena) o que quedaron varados en ella."""
        rows = await self._pool.fetch(
            """
            SELECT id, direction, channel FROM messages
             WHERE status IN ('received', 'pending')
               AND created_at < now() - make_interval(secs => $1)
               AND (enqueued_at IS NULL OR enqueued_at < now() - make_interval(secs => $2))
             ORDER BY created_at
             LIMIT $3
            """,
            float(older_than_seconds),
            float(stale_after_seconds),
            limit,
        )
        return [PendingJob(str(row["id"]), row["direction"], row["channel"]) for row in rows]

    async def mark_enqueued(self, message_ids: list[str]) -> None:
        if message_ids:
            await self._pool.execute(
                "UPDATE messages SET enqueued_at = now() WHERE id = ANY($1::uuid[])",
                [uuid.UUID(item) for item in message_ids],
            )

    async def expired_handoffs(self, minutes: int) -> list[str]:
        """Conversaciones esperando una persona hace demasiado (desde el pedido, aunque la persona
        siga escribiendo): vuelven al bot."""
        rows = await self._pool.fetch(
            """
            UPDATE conversations SET state = 'bot', assigned_to = NULL, handoff_reason = 'timeout',
                   last_activity_at = now()
             WHERE state = 'waiting_human'
               AND COALESCE(handoff_at, last_activity_at) < now() - make_interval(mins => $1)
            RETURNING id
            """,
            minutes,
        )
        return [str(row["id"]) for row in rows]

    async def close_stale_human_conversations(self, days: int = 7) -> int:
        result = await self._pool.execute(
            """
            UPDATE conversations SET state = 'closed', closed_at = now(), close_reason = 'stale', assigned_to = NULL
             WHERE state = 'human' AND last_activity_at < now() - make_interval(days => $1)
            """,
            days,
        )
        return int(result.split()[-1])

    async def erase_requested_contacts(self, grace_minutes: int = 5) -> int:
        result = await self._pool.execute(
            "DELETE FROM contacts WHERE erase_requested_at < now() - make_interval(mins => $1)", grace_minutes
        )
        return int(result.split()[-1])

    async def apply_retention(self, policy: RetentionPolicy) -> dict[str, int]:
        deleted: dict[str, int] = {}
        async with self._pool.acquire() as conn:
            for name, sql, days in (
                (
                    "messages",
                    "DELETE FROM messages WHERE created_at < now() - make_interval(days => $1)",
                    policy.messages_days,
                ),
                (
                    "conversations",
                    "DELETE FROM conversations WHERE state = 'closed' "
                    "AND closed_at < now() - make_interval(days => $1)",
                    policy.messages_days,
                ),
                (
                    "contacts",
                    "DELETE FROM contacts WHERE last_seen_at < now() - make_interval(days => $1)",
                    policy.contacts_days,
                ),
                ("audit_log", "DELETE FROM audit_log WHERE at < now() - make_interval(days => $1)", policy.audit_days),
            ):
                result = await conn.execute(sql, days)
                deleted[name] = int(result.split()[-1])
            result = await conn.execute("DELETE FROM link_codes WHERE expires_at < now()")
            deleted["link_codes"] = int(result.split()[-1])
            result = await conn.execute(
                "DELETE FROM persons p WHERE NOT EXISTS (SELECT 1 FROM contacts c WHERE c.person_id = p.id)"
            )
            deleted["persons"] = int(result.split()[-1])
        return deleted

    async def retire_old_kb_versions(self, keep: int = 2) -> list[str]:
        """Borra tablas de versiones viejas de la base de conocimiento (se conservan `keep`)."""
        rows = await self._pool.fetch(
            """
            SELECT version, table_name FROM kb_versions
             WHERE status IN ('retired', 'failed', 'ready')
               AND version NOT IN (SELECT version FROM kb_active)
             ORDER BY created_at DESC OFFSET $1
            """,
            max(keep - 1, 0),
        )
        dropped: list[str] = []
        async with self._pool.acquire() as conn:
            for row in rows:
                table = row["table_name"]
                if not table.startswith("kb_chunks_") or not table[10:].isdigit():
                    continue
                async with conn.transaction():
                    await conn.execute(f'DROP TABLE IF EXISTS "{table}"')
                    await conn.execute("DELETE FROM kb_versions WHERE version = $1", row["version"])
                dropped.append(row["version"])
        return dropped
