"""Pool de conexiones a PostgreSQL y migraciones del esquema."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import asyncpg

from iamia.rag.store import ensure_semantic_cache_table

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 7_274_101  # advisory lock: una sola réplica migra a la vez


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    statement_timeout_ms: int = 5000,
    application_name: str = "iamia",
) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
        command_timeout=max(1.0, statement_timeout_ms / 1000 * 2),
        server_settings={
            "statement_timeout": str(int(statement_timeout_ms)),
            "idle_in_transaction_session_timeout": "30000",
            "application_name": application_name,
        },
    )
    if pool is None:  # pragma: no cover - asyncpg siempre devuelve el pool
        raise RuntimeError("no se pudo crear el pool de PostgreSQL")
    return pool


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


async def migrate(pool: asyncpg.Pool, *, embedding_dimensions: int) -> list[str]:
    """Aplica en orden las migraciones pendientes, cada una en su transacción."""
    applied_now: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row["version"]: row["checksum"]
                for row in await conn.fetch("SELECT version, checksum FROM schema_migrations")
            }
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                checksum = _checksum(sql)
                if path.stem in applied:
                    if applied[path.stem] != checksum:
                        raise RuntimeError(f"la migración {path.stem} cambió después de aplicarse")
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)", path.stem, checksum
                    )
                applied_now.append(path.stem)
                logger.info("migration_applied", extra={"ctx": {"version": path.stem}})
            await ensure_semantic_cache_table(conn, embedding_dimensions)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK)
    return applied_now
