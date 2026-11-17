"""Recifrado con la clave activa de los datos guardados con claves anteriores.

Después de rotar DATA_ENCRYPTION_KEYS (la clave nueva primero, las anteriores detrás), lo nuevo se
cifra con la clave nueva y lo viejo se sigue leyendo con las anteriores. El mantenimiento recorre
cada tabla con datos cifrados por clave primaria, en lotes, y recifra lo que todavía usa otra
clave. El progreso queda en key_rotation: si el proceso se reinicia, sigue desde donde estaba.

Una tabla queda al día con una vuelta completa sin nada para recifrar que empezó después de un
margen (`grace_seconds`) desde que se vio la clave nueva. Durante un despliegue gradual, los
procesos que todavía tienen la configuración anterior siguen cifrando con la clave vieja: esa
última vuelta los alcanza. Antes de retirar la clave vieja se confirma con
`python -m iamia key-usage`. Con una sola clave en el anillo (una instalación nueva) no hay margen
que esperar: un proceso que cifrara con otra clave escribiría datos que igual no se pueden leer.

Cada fila se actualiza solo si nadie la cambió mientras tanto (por ejemplo, un token de X que se
renovó en el medio): nunca se pisa un dato nuevo con uno viejo recifrado.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from iamia.core.metrics import KEY_ROTATION_ROWS
from iamia.persistence.codec import DataCodec

logger = logging.getLogger(__name__)

# De la más chica a la más grande: las chicas quedan al día enseguida.
TARGETS = ("oauth_tokens", "contacts", "messages")
_FIRST_UUID = uuid.UUID(int=0)

# Cada página trae el lote entero (la lectura queda acotada aunque no haya nada que recifrar),
# pero el dato cifrado solo cuando usa otra clave: lo demás no viaja.
_PAGES = {
    "oauth_tokens": """
        SELECT provider AS key, refresh_token_enc,
               CASE WHEN split_part(access_token_enc, ':', 2) <> $3
                      OR split_part(refresh_token_enc, ':', 2) <> $3 THEN access_token_enc END AS token
          FROM oauth_tokens WHERE provider > $1 ORDER BY provider LIMIT $2
    """,
    "contacts": """
        SELECT id::text AS key, channel, address_hash,
               CASE WHEN split_part(address_enc, ':', 2) <> $3 THEN address_enc END AS token
          FROM contacts WHERE id > $1 ORDER BY id LIMIT $2
    """,
    "messages": """
        SELECT id::text AS key, CASE WHEN split_part(body_enc, ':', 2) <> $3 THEN body_enc END AS token
          FROM messages WHERE id > $1 ORDER BY id LIMIT $2
    """,
}

# Solo si el valor sigue siendo el que se leyó.
_UPDATES = {
    "oauth_tokens": """
        UPDATE oauth_tokens AS t SET access_token_enc = v.new_access, refresh_token_enc = v.new_refresh
          FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[])
               AS v(provider, old_access, old_refresh, new_access, new_refresh)
         WHERE t.provider = v.provider AND t.access_token_enc = v.old_access
           AND t.refresh_token_enc = v.old_refresh
    """,
    "contacts": """
        UPDATE contacts AS t SET address_enc = v.new_value
          FROM unnest($1::uuid[], $2::text[], $3::text[]) AS v(id, old_value, new_value)
         WHERE t.id = v.id AND t.address_enc = v.old_value
    """,
    "messages": """
        UPDATE messages AS t SET body_enc = v.new_value
          FROM unnest($1::uuid[], $2::text[], $3::text[]) AS v(id, old_value, new_value)
         WHERE t.id = v.id AND t.body_enc = v.old_value
    """,
}

_USAGE = {
    "oauth_tokens": """
        SELECT key_id, count(*) AS total FROM (
            SELECT split_part(access_token_enc, ':', 2) AS key_id FROM oauth_tokens
             UNION ALL
            SELECT split_part(refresh_token_enc, ':', 2) FROM oauth_tokens
        ) AS tokens GROUP BY key_id
    """,
    "contacts": "SELECT split_part(address_enc, ':', 2) AS key_id, count(*) AS total FROM contacts GROUP BY 1",
    "messages": (
        "SELECT split_part(body_enc, ':', 2) AS key_id, count(*) AS total FROM messages "
        "WHERE body_enc IS NOT NULL GROUP BY 1"
    ),
}


@dataclass(frozen=True, slots=True)
class RotationStep:
    target: str
    rotated: int = 0
    failed: int = 0
    finished: bool = False  # la tabla quedó al día con la clave activa
    waiting: bool = False  # vuelta terminada; la próxima empieza cuando venza el margen


class KeyRotationRepository:
    def __init__(
        self, pool: asyncpg.Pool, codec: DataCodec, *, batch_size: int = 1000, grace_seconds: float = 3600
    ) -> None:
        self._pool = pool
        self._codec = codec
        self._batch_size = batch_size
        self._grace = float(grace_seconds) if len(codec.key_ids) > 1 else 0.0

    @property
    def key_id(self) -> str:
        return self._codec.active_key_id

    async def pending(self) -> list[str]:
        """Tablas que todavía no quedaron al día con la clave activa."""
        rows = await self._pool.fetch(
            "SELECT target FROM key_rotation WHERE key_id = $1 AND finished_at IS NOT NULL", self.key_id
        )
        done = {row["target"] for row in rows}
        return [target for target in TARGETS if target not in done]

    async def step(self, target: str) -> RotationStep:
        """Recifra un lote de `target` y guarda por dónde va."""
        state = await self._state(target)
        if state["finished_at"] is not None:
            return RotationStep(target, finished=True)
        if state["waiting"]:
            return RotationStep(target, waiting=True)
        start = state["last_id"]
        after: Any = (start or "") if target == "oauth_tokens" else (uuid.UUID(start) if start else _FIRST_UUID)
        rows = await self._pool.fetch(_PAGES[target], after, self._batch_size, self.key_id)
        columns, failed = self._rotate(target, rows)
        rotated = 0
        if columns[0]:
            result = await self._pool.execute(_UPDATES[target], *columns)
            rotated = int(result.split()[-1])
            KEY_ROTATION_ROWS.labels(target, "rotated").inc(rotated)
        if failed:
            KEY_ROTATION_ROWS.labels(target, "failed").inc(failed)
            logger.warning("key_rotation_undecryptable", extra={"ctx": {"table": target, "count": failed}})
        if len(rows) == self._batch_size:
            await self._pool.execute(
                """
                UPDATE key_rotation SET last_id = $2, rotated = rotated + $3, failed = failed + $4, updated_at = now()
                 WHERE target = $1
                """,
                target,
                rows[-1]["key"],
                rotated,
                failed,
            )
            return RotationStep(target, rotated, failed)
        return await self._end_pass(target, state, rotated, failed)

    async def _state(self, target: str) -> asyncpg.Record:
        # Primera vez o clave activa distinta: la rotación de esta tabla empieza de cero.
        restarted = await self._pool.fetchval(
            """
            INSERT INTO key_rotation (target, key_id) VALUES ($1, $2)
            ON CONFLICT (target) DO UPDATE
               SET key_id = EXCLUDED.key_id, started_at = now(), pass_started_at = now(), last_id = NULL,
                   rotated = 0, failed = 0, finished_at = NULL, updated_at = now()
             WHERE key_rotation.key_id <> EXCLUDED.key_id
            RETURNING key_id
            """,
            target,
            self.key_id,
        )
        if restarted:
            logger.info("key_rotation_started", extra={"ctx": {"table": target, "key_id": self.key_id}})
        row = await self._pool.fetchrow(
            """
            SELECT last_id, rotated, failed, finished_at, pass_started_at > now() AS waiting,
                   pass_started_at >= started_at + make_interval(secs => $2) AS after_grace
              FROM key_rotation WHERE target = $1
            """,
            target,
            self._grace,
        )
        if row is None:  # pragma: no cover - la fila se acaba de crear
            raise RuntimeError(f"sin estado de rotación para {target}")
        return row

    def _rotate(self, target: str, rows: list[asyncpg.Record]) -> tuple[list[list[Any]], int]:
        """Parámetros del UPDATE con lo recifrado, y cuántos datos no se pudieron descifrar."""
        columns: list[list[Any]] = [[] for _ in range(5 if target == "oauth_tokens" else 3)]
        failed = 0
        for row in rows:
            token = row["token"]
            if token is None:
                continue
            try:
                if target == "oauth_tokens":
                    provider, refresh = row["key"], row["refresh_token_enc"]
                    values: tuple[Any, ...] = (
                        provider,
                        token,
                        refresh,
                        self._codec.rotate_secret(f"{provider}:access", token),
                        self._codec.rotate_secret(f"{provider}:refresh", refresh),
                    )
                elif target == "contacts":
                    new = self._codec.rotate_address(row["channel"], row["address_hash"], token)
                    values = (uuid.UUID(row["key"]), token, new)
                else:
                    values = (uuid.UUID(row["key"]), token, self._codec.rotate_text(row["key"], token))
            except ValueError:  # clave que ya no está en el anillo o dato alterado
                failed += 1
                continue
            for column, value in zip(columns, values, strict=True):
                column.append(value)
        return columns, failed

    async def _end_pass(self, target: str, state: asyncpg.Record, rotated: int, failed: int) -> RotationStep:
        total_failed = state["failed"] + failed
        if state["rotated"] + rotated == 0 and state["after_grace"]:
            await self._pool.execute(
                """
                UPDATE key_rotation SET last_id = NULL, failed = $2, finished_at = now(), updated_at = now()
                 WHERE target = $1
                """,
                target,
                total_failed,
            )
            log = logger.warning if total_failed else logger.info
            log(
                "key_rotation_table_done",
                extra={"ctx": {"table": target, "key_id": self.key_id, "undecryptable": total_failed}},
            )
            return RotationStep(target, rotated, failed, finished=True)
        # Otra vuelta: enseguida si ya venció el margen, si no cuando venza.
        waiting = await self._pool.fetchval(
            """
            UPDATE key_rotation
               SET last_id = NULL, rotated = 0, failed = 0, updated_at = now(),
                   pass_started_at = GREATEST(now(), started_at + make_interval(secs => $2))
             WHERE target = $1
            RETURNING pass_started_at > now()
            """,
            target,
            self._grace,
        )
        return RotationStep(target, rotated, failed, waiting=bool(waiting))


async def key_usage(pool: asyncpg.Pool) -> dict[str, dict[str, int]]:
    """Cuántos datos cifrados usa cada clave, por tabla. Recorre las tablas enteras: es para
    consultar a mano antes de retirar una clave, no para correr seguido."""
    usage: dict[str, dict[str, int]] = {}
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '30min'")
        for target in TARGETS:
            usage[target] = {row["key_id"]: row["total"] for row in await conn.fetch(_USAGE[target])}
    return usage


async def rotation_status(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT target, key_id, started_at, finished_at, failed FROM key_rotation ORDER BY target")
