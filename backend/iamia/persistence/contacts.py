"""Acciones sobre contactos pedidas por la propia persona (baja, borrado, vinculación) y auditoría."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class ContactRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def set_opt_out(self, contact_id: str, opted_out: bool) -> None:
        await self._pool.execute(
            "UPDATE contacts SET opted_out = $2, last_seen_at = now() WHERE id = $1", uuid.UUID(contact_id), opted_out
        )

    async def request_erasure(self, contact_id: str) -> None:
        # El borrado efectivo lo hace el mantenimiento unos minutos después, cuando ya se envió
        # la confirmación (si se borrara ahora no habría a quién responder).
        await self._pool.execute(
            "UPDATE contacts SET erase_requested_at = COALESCE(erase_requested_at, now()) WHERE id = $1",
            uuid.UUID(contact_id),
        )

    async def create_link_code(self, contact_id: str, code_hash: str, ttl_seconds: int) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM link_codes WHERE contact_id = $1", uuid.UUID(contact_id))
            await conn.execute(
                "INSERT INTO link_codes (code_hash, contact_id, expires_at) "
                "VALUES ($1, $2, now() + make_interval(secs => $3))",
                code_hash,
                uuid.UUID(contact_id),
                float(ttl_seconds),
            )

    async def redeem_link_code(self, code_hash: str, contact_id: str) -> bool:
        """Vincula el contacto que generó el código con el que lo canjea (mismo `person_id`)."""
        async with self._pool.acquire() as conn, conn.transaction():
            source = await conn.fetchval(
                "DELETE FROM link_codes WHERE code_hash = $1 AND expires_at > now() RETURNING contact_id", code_hash
            )
            if source is None or str(source) == contact_id:
                return False
            rows = await conn.fetch(
                "SELECT id, person_id FROM contacts WHERE id = ANY($1::uuid[]) FOR UPDATE",
                [source, uuid.UUID(contact_id)],
            )
            if len(rows) != 2:
                return False
            persons = [row["person_id"] for row in rows if row["person_id"] is not None]
            person = persons[0] if persons else uuid.uuid4()
            if not persons:
                await conn.execute("INSERT INTO persons (id) VALUES ($1)", person)
            # Si ambos ya tenían persona, se unifican en la primera.
            others = [p for p in persons[1:] if p != person]
            if others:
                await conn.execute(
                    "UPDATE contacts SET person_id = $1 WHERE person_id = ANY($2::uuid[])", person, others
                )
            await conn.execute(
                "UPDATE contacts SET person_id = $1 WHERE id = ANY($2::uuid[])", person, [source, uuid.UUID(contact_id)]
            )
        return True


class AuditRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(
        self,
        actor: str,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._pool.execute(
                "INSERT INTO audit_log (actor, action, target_type, target_id, detail) VALUES ($1, $2, $3, $4, $5)",
                actor[:200],
                action[:100],
                target_type,
                target_id,
                detail or {},
            )
        except (OSError, asyncpg.PostgresError) as exc:
            # La auditoría no debe tumbar la operación, pero su falla queda registrada.
            logger.error("audit_write_failed", extra={"ctx": {"action": action, "error": type(exc).__name__}})

    async def list(self, *, limit: int = 100, before_id: int | None = None) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT id, at, actor, action, target_type, target_id, detail FROM audit_log
             WHERE ($1::bigint IS NULL OR id < $1) ORDER BY id DESC LIMIT $2
            """,
            before_id,
            min(max(limit, 1), 500),
        )
        return [dict(row) | {"at": row["at"].isoformat()} for row in rows]
