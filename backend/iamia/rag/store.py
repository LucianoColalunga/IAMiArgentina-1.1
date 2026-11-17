"""Base de conocimiento y caché semántica sobre PostgreSQL + pgvector.

Cada ingesta crea una tabla nueva (kb_chunks_<AAAAMMDDhhmmss>) con su índice HNSW y, recién
cuando está completa y verificada, mueve el puntero `kb_active` en una transacción (blue/green).
Las réplicas de la API releen el puntero cada KB_REFRESH_SECONDS: la reindexación no corta
el servicio y la versión anterior queda disponible para volver atrás.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence

import asyncpg

from iamia.rag.models import KBInfo, RetrievedChunk

logger = logging.getLogger(__name__)

KB_TABLE_PATTERN = re.compile(r"^kb_chunks_\d{14}$")


def to_pgvector(values: Sequence[float]) -> str:
    """Literal de pgvector ("[0.1,0.2,...]"). Se pasa como parámetro y se castea a vector en SQL."""
    return "[" + ",".join(f"{float(value):.7g}" for value in values) + "]"


def semantic_cache_table(dimensions: int) -> str:
    return f"semantic_cache_d{int(dimensions)}"


class PgVectorKnowledgeBase:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        refresh_seconds: float = 30.0,
        ef_search: int = 64,
        expected_embed_model: str | None = None,
    ) -> None:
        self._pool = pool
        self._refresh = refresh_seconds
        self._ef_search = max(10, int(ef_search))
        self._expected_model = expected_embed_model
        self._cached: KBInfo | None = None
        self._cached_at = float("-inf")
        self._lock = asyncio.Lock()
        self._warned_mismatch = False

    def _invalidate(self) -> None:
        self._cached_at = float("-inf")

    async def active(self) -> KBInfo | None:
        if time.monotonic() - self._cached_at < self._refresh:
            return self._cached
        async with self._lock:
            if time.monotonic() - self._cached_at < self._refresh:
                return self._cached
            row = await self._pool.fetchrow(
                """
                SELECT v.version, v.table_name, v.embed_model, v.dimensions
                FROM kb_active a JOIN kb_versions v ON v.version = a.version
                """
            )
            info: KBInfo | None = None
            if row is not None and KB_TABLE_PATTERN.match(row["table_name"]):
                info = KBInfo(row["version"], row["table_name"], row["embed_model"], row["dimensions"])
                if self._expected_model and info.embed_model != self._expected_model and not self._warned_mismatch:
                    self._warned_mismatch = True
                    logger.error(
                        "embedding_model_mismatch",
                        extra={"ctx": {"index_model": info.embed_model, "configured_model": self._expected_model}},
                    )
            if info and self._cached and info.version != self._cached.version:
                logger.info("kb_version_changed", extra={"ctx": {"from": self._cached.version, "to": info.version}})
            self._cached, self._cached_at = info, time.monotonic()
            return info

    async def search(self, kb: KBInfo, embedding: Sequence[float], top_k: int) -> list[RetrievedChunk]:
        if not KB_TABLE_PATTERN.match(kb.table):
            raise ValueError("nombre de tabla de conocimiento inválido")
        # El nombre de la tabla se valida con una expresión estricta: no hay forma de inyectar SQL.
        query = (
            f"SELECT id, title, url, content, 1 - (embedding <=> $1::vector) AS score "  # noqa: S608
            f'FROM "{kb.table}" ORDER BY embedding <=> $1::vector LIMIT $2'
        )
        vector = to_pgvector(embedding)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {self._ef_search}")
                rows = await conn.fetch(query, vector, top_k)
        except asyncpg.UndefinedTableError:
            # La versión cacheada fue retirada por una ingesta nueva: se relee el puntero.
            self._invalidate()
            raise
        return [
            RetrievedChunk(
                id=row["id"],
                text=row["content"],
                title=row["title"] or "",
                url=row["url"] or "",
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def ping(self) -> bool:
        try:
            return await self._pool.fetchval("SELECT 1") == 1
        except (OSError, asyncpg.PostgresError):
            return False


class PgVectorSemanticCache:
    """Encuentra preguntas ya respondidas que significan lo mismo ("cómo saco el DNI digital" ~
    "de qué forma obtengo el DNI en el celular") y devuelve la clave de la respuesta cacheada."""

    def __init__(self, pool: asyncpg.Pool, *, dimensions: int, threshold: float, ttl_seconds: int) -> None:
        self._pool = pool
        self._table = semantic_cache_table(dimensions)
        self._threshold = threshold
        self._ttl = ttl_seconds

    async def lookup(self, embedding: Sequence[float], scope: str) -> str | None:
        query = (
            f"SELECT answer_key, 1 - (embedding <=> $1::vector) AS similarity FROM {self._table} "  # noqa: S608
            "WHERE scope = $2 AND expires_at > now() ORDER BY embedding <=> $1::vector LIMIT 1"
        )
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                # Búsqueda iterativa: el filtro por scope no deja al índice sin candidatos.
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                row = await conn.fetchrow(query, to_pgvector(embedding), scope)
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning("semantic_cache_unavailable", extra={"ctx": {"op": "lookup", "error": type(exc).__name__}})
            return None
        if row is None or float(row["similarity"]) < self._threshold:
            return None
        return str(row["answer_key"])

    async def store(self, embedding: Sequence[float], scope: str, answer_key: str) -> None:
        query = (
            f"INSERT INTO {self._table} (answer_key, scope, embedding, expires_at) "  # noqa: S608
            "VALUES ($1, $2, $3::vector, now() + make_interval(secs => $4)) "
            "ON CONFLICT (answer_key) DO UPDATE SET scope = EXCLUDED.scope, embedding = EXCLUDED.embedding, "
            "expires_at = EXCLUDED.expires_at"
        )
        try:
            await self._pool.execute(query, answer_key, scope, to_pgvector(embedding), float(self._ttl))
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning("semantic_cache_unavailable", extra={"ctx": {"op": "store", "error": type(exc).__name__}})

    async def purge(self) -> int:
        """Borra entradas vencidas. Lo corre el worker de mantenimiento."""
        result = await self._pool.execute(f"DELETE FROM {self._table} WHERE expires_at < now()")  # noqa: S608
        return int(result.split()[-1]) if result else 0


async def ensure_semantic_cache_table(conn: asyncpg.Connection, dimensions: int) -> None:
    """Crea la tabla de la caché semántica para la dimensión del modelo de embeddings configurado."""
    table = semantic_cache_table(dimensions)
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            answer_key text PRIMARY KEY,
            scope text NOT NULL,
            embedding vector({int(dimensions)}) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS {table}_expires ON {table} (expires_at);
        """
    )
