"""Indexación azul/verde en pgvector, búsqueda, caché semántica y vuelta atrás."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

import asyncpg
import pytest

from iamia.rag.ingest.chunking import build_chunks, digest
from iamia.rag.ingest.indexer import IndexValidationError, KnowledgeIndexer
from iamia.rag.ingest.sources import FaqEntry
from iamia.rag.store import PgVectorKnowledgeBase, PgVectorSemanticCache
from tests.integration.conftest import DIMENSIONS

_WORD = re.compile(r"[a-záéíóúñ0-9]+")


class HashEmbedder:
    """Embeddings deterministas por palabras: textos con palabras en común quedan cerca."""

    model_name = "hash-test"

    def __init__(self, dimensions: int = DIMENSIONS, broken: bool = False) -> None:
        self._dims = dimensions
        self._broken = broken

    def _vector(self, text: str) -> list[float]:
        values = [0.0] * self._dims
        for word in _WORD.findall(text.lower()):
            bucket = int(hashlib.sha256(word.encode()).hexdigest(), 16) % self._dims
            values[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._broken:
            return [[0.0] * (self._dims - 1) for _ in texts]
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


ENTRIES = [
    FaqEntry(
        "¿Cómo saco el DNI digital?",
        "El DNI digital se activa desde la app Mi Argentina validando tu identidad.",
        "https://www.argentina.gob.ar/dni",
        "DNI",
    ),
    FaqEntry(
        "¿Cómo renuevo la licencia de conducir?",
        "La licencia se renueva con turno previo en tu municipio.",
        "https://www.argentina.gob.ar/licencia",
        "Licencia de conducir",
    ),
    FaqEntry(
        "¿Qué es la cédula azul digital?",
        "La cédula digital del vehículo se ve en la app y reemplaza a la tarjeta.",
        "https://www.argentina.gob.ar/cedula",
        "Cédula del vehículo",
    ),
]


@pytest.fixture
async def clean_kb(pool: asyncpg.Pool) -> asyncpg.Pool:
    tables = await pool.fetch("SELECT table_name FROM kb_versions")
    await pool.execute("DELETE FROM kb_active")
    await pool.execute("DELETE FROM kb_versions")
    for row in tables:
        await pool.execute(f'DROP TABLE IF EXISTS "{row["table_name"]}"')
    return pool


async def test_build_activate_search_and_rollback(clean_kb: asyncpg.Pool) -> None:
    embedder = HashEmbedder()
    indexer = KnowledgeIndexer(clean_kb, embedder, dimensions=DIMENSIONS, batch_size=2)
    chunks = build_chunks(ENTRIES)
    first = await indexer.build(chunks, source_digest=digest(chunks))
    await indexer.activate(first)

    kb = PgVectorKnowledgeBase(clean_kb, refresh_seconds=0)
    info = await kb.active()
    assert info is not None and info.version == first and info.dimensions == DIMENSIONS
    results = await kb.search(info, await embedder.embed_query("¿cómo activo el dni digital?"), 2)
    assert results[0].url == "https://www.argentina.gob.ar/dni"
    assert results[0].score > results[1].score

    # Segunda versión (otro contenido) y vuelta atrás a la primera.
    import asyncio

    await asyncio.sleep(1.1)  # la versión tiene resolución de segundos
    second_chunks = build_chunks(ENTRIES[:2])
    second = await indexer.build(second_chunks, source_digest=digest(second_chunks))
    await indexer.activate(second)
    assert (await indexer.active())["version"] == second  # type: ignore[index]
    assert await indexer.rollback() == first
    assert (await indexer.active())["version"] == first  # type: ignore[index]


async def test_invalid_embeddings_leave_no_half_built_index(clean_kb: asyncpg.Pool) -> None:
    indexer = KnowledgeIndexer(clean_kb, HashEmbedder(broken=True), dimensions=DIMENSIONS)
    chunks = build_chunks(ENTRIES)
    with pytest.raises(IndexValidationError):
        await indexer.build(chunks, source_digest="x")
    assert await clean_kb.fetchval("SELECT status FROM kb_versions") == "failed"
    tables = await clean_kb.fetchval("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'kb_chunks_%'")
    assert tables == 0
    assert await indexer.active() is None


async def test_semantic_cache_finds_similar_questions(pool: asyncpg.Pool) -> None:
    embedder = HashEmbedder()
    cache = PgVectorSemanticCache(pool, dimensions=DIMENSIONS, threshold=0.9, ttl_seconds=60)
    await cache.store(await embedder.embed_query("como saco el dni digital"), "scope-1", "answer-key-1")
    assert await cache.lookup(await embedder.embed_query("como saco el dni digital?"), "scope-1") == "answer-key-1"
    assert await cache.lookup(await embedder.embed_query("como saco el dni digital"), "scope-2") is None
    assert await cache.lookup(await embedder.embed_query("renovar licencia de conducir"), "scope-1") is None
