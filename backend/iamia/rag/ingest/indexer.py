"""Indexación azul/verde en pgvector: reindexar sin cortar el servicio.

1. Se crea una tabla nueva kb_chunks_<versión> y se cargan los fragmentos con sus embeddings.
2. Se construye el índice HNSW y se valida: cantidad exacta y una búsqueda de prueba.
3. Recién entonces se cambia el puntero kb_active en una transacción. Las réplicas ven la
   versión nueva en su próxima lectura del puntero (KB_REFRESH_SECONDS) y la caché queda
   invalidada sola porque la versión forma parte de la clave.
4. La versión anterior queda disponible para volver atrás (`ingest --rollback`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import asyncpg

from iamia.ai.embeddings import Embedder
from iamia.core.clock import utcnow
from iamia.rag.ingest.chunking import Chunk
from iamia.rag.store import KB_TABLE_PATTERN, to_pgvector

logger = logging.getLogger(__name__)


class IndexValidationError(RuntimeError):
    pass


class KnowledgeIndexer:
    def __init__(self, pool: asyncpg.Pool, embedder: Embedder, *, dimensions: int, batch_size: int = 32) -> None:
        self._pool = pool
        self._embedder = embedder
        self._dimensions = dimensions
        self._batch = max(1, batch_size)

    async def active(self) -> dict[str, object] | None:
        row = await self._pool.fetchrow(
            """
            SELECT v.version, v.table_name, v.embed_model, v.chunks, v.source_digest
              FROM kb_active a JOIN kb_versions v ON v.version = a.version
            """
        )
        return dict(row) if row else None

    async def _embed(self, texts: Sequence[str], attempts: int = 6) -> list[list[float]]:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                vectors = await self._embedder.embed(texts)
            except Exception as exc:
                if attempt == attempts:
                    raise
                logger.warning("embed_retry", extra={"ctx": {"attempt": attempt, "error": type(exc).__name__}})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if len(vectors) != len(texts) or any(len(v) != self._dimensions for v in vectors):
                raise IndexValidationError("el modelo de embeddings devolvió dimensiones inesperadas")
            return vectors
        raise RuntimeError("inalcanzable")

    async def build(self, chunks: Sequence[Chunk], *, source_digest: str) -> str:
        if not chunks:
            raise IndexValidationError("no hay fragmentos para indexar")
        version = utcnow().strftime("%Y%m%d%H%M%S")
        table = f"kb_chunks_{version}"
        if not KB_TABLE_PATTERN.match(table):
            raise IndexValidationError("nombre de versión inválido")
        dims = int(self._dimensions)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO kb_versions (version, table_name, embed_model, dimensions, source_digest, status)
                VALUES ($1, $2, $3, $4, $5, 'building')
                """,
                version,
                table,
                self._embedder.model_name,
                dims,
                source_digest,
            )
            await conn.execute(
                f"""
                CREATE TABLE "{table}" (
                    id text PRIMARY KEY,
                    title text,
                    url text,
                    content text NOT NULL,
                    question text,
                    category text,
                    source text,
                    embedding vector({dims}) NOT NULL
                )
                """
            )
        try:
            for start in range(0, len(chunks), self._batch):
                batch = chunks[start : start + self._batch]
                vectors = await self._embed([chunk.content for chunk in batch])
                await self._pool.executemany(
                    f'INSERT INTO "{table}" (id, title, url, content, question, category, source, embedding) '  # noqa: S608
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)",
                    [
                        (c.id, c.title, c.url, c.content, c.question, c.category, c.source, to_pgvector(v))
                        for c, v in zip(batch, vectors, strict=True)
                    ],
                )
                logger.info(
                    "index_progress",
                    extra={"ctx": {"done": min(start + self._batch, len(chunks)), "total": len(chunks)}},
                )
            async with self._pool.acquire() as conn:
                await conn.execute("SET maintenance_work_mem = '256MB'")
                await conn.execute(
                    f'CREATE INDEX "{table}_hnsw" ON "{table}" USING hnsw (embedding vector_cosine_ops) '
                    "WITH (m = 16, ef_construction = 64)"
                )
                await conn.execute(f'ANALYZE "{table}"')
            await self._validate(table, chunks)
        except Exception:
            await self._pool.execute("UPDATE kb_versions SET status = 'failed' WHERE version = $1", version)
            await self._pool.execute(f'DROP TABLE IF EXISTS "{table}"')
            raise
        documents = len({(c.url, c.question) for c in chunks})
        await self._pool.execute(
            "UPDATE kb_versions SET status = 'ready', documents = $2, chunks = $3 WHERE version = $1",
            version,
            documents,
            len(chunks),
        )
        return version

    async def _validate(self, table: str, chunks: Sequence[Chunk]) -> None:
        count = await self._pool.fetchval(f'SELECT count(*) FROM "{table}"')  # noqa: S608
        if count != len(chunks):
            raise IndexValidationError(f"se esperaban {len(chunks)} fragmentos y hay {count}")
        probe = chunks[0]
        vector = (await self._embed([probe.content]))[0]
        top = await self._pool.fetchval(
            f'SELECT id FROM "{table}" ORDER BY embedding <=> $1::vector LIMIT 1',  # noqa: S608
            to_pgvector(vector),
        )
        if top != probe.id:
            # Puede haber fragmentos casi idénticos; se acepta si el más cercano es equivalente.
            similarity = await self._pool.fetchval(
                f'SELECT 1 - (embedding <=> $1::vector) FROM "{table}" WHERE id = $2',  # noqa: S608
                to_pgvector(vector),
                probe.id,
            )
            if similarity is None or float(similarity) < 0.98:
                raise IndexValidationError("la búsqueda de prueba no encontró el fragmento esperado")

    async def activate(self, version: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            status = await conn.fetchval("SELECT status FROM kb_versions WHERE version = $1 FOR UPDATE", version)
            if status not in ("ready", "retired", "active"):
                raise IndexValidationError(f"la versión {version} no está lista ({status})")
            await conn.execute(
                "UPDATE kb_versions SET status = 'retired' WHERE status = 'active' AND version <> $1", version
            )
            await conn.execute(
                "UPDATE kb_versions SET status = 'active', activated_at = now() WHERE version = $1", version
            )
            await conn.execute(
                """
                INSERT INTO kb_active (singleton, version) VALUES (true, $1)
                ON CONFLICT (singleton) DO UPDATE SET version = EXCLUDED.version, updated_at = now()
                """,
                version,
            )
        logger.info("kb_activated", extra={"ctx": {"version": version}})

    async def rollback(self) -> str | None:
        previous = await self._pool.fetchval(
            "SELECT version FROM kb_versions WHERE status = 'retired' ORDER BY activated_at DESC NULLS LAST LIMIT 1"
        )
        if previous is None:
            return None
        await self.activate(previous)
        return str(previous)
