"""Comandos de datos: migraciones, ingesta de la base de conocimiento y uso de las claves de cifrado."""

from __future__ import annotations

import argparse
import logging

from iamia.ai.embeddings import OpenAICompatibleEmbedder
from iamia.core.logs import configure_logging
from iamia.persistence.database import create_pool, migrate
from iamia.persistence.rotation import key_usage, rotation_status
from iamia.rag.ingest.indexer import KnowledgeIndexer
from iamia.rag.ingest.pipeline import IngestOptions, IngestReport, run_ingest
from iamia.security.crypto import FieldCipher
from iamia.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def _pool(settings: Settings, role: str):
    # La indexación crea índices HNSW: necesita un statement_timeout mucho mayor que la API.
    return await create_pool(
        settings.database_url.get_secret_value(),
        min_size=1,
        max_size=4,
        statement_timeout_ms=30 * 60 * 1000,
        application_name=f"iamia-{role}",
    )


async def migrate_command() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, role="migrate")
    pool = await _pool(settings, "migrate")
    try:
        applied = await migrate(pool, embedding_dimensions=settings.embedding_dimensions)
    finally:
        await pool.close()
    print(f"migraciones aplicadas: {', '.join(applied) if applied else 'ninguna (la base ya estaba al día)'}")
    return 0


async def key_usage_command() -> int:
    """Cuántos datos cifrados usa cada clave. Una clave anterior se saca de DATA_ENCRYPTION_KEYS
    recién cuando no aparece acá: si no, lo que quedó cifrado con ella no se podría leer más."""
    settings = get_settings()
    configure_logging(settings.log_level, role="key-usage")
    active = FieldCipher.from_config(settings.data_encryption_keys.get_secret_value()).active_key_id
    pool = await _pool(settings, "key-usage")
    try:
        usage = await key_usage(pool)
        status = {row["target"]: row for row in await rotation_status(pool)}
    finally:
        await pool.close()
    print(f"clave activa: {active}")
    others = 0
    for table, counts in usage.items():
        detail = ", ".join(f"{key or '?'}: {total}" for key, total in sorted(counts.items())) or "sin datos"
        row = status.get(table)
        if row is None or row["key_id"] != active:
            state = "recifrado sin empezar"
        elif row["finished_at"] is None:
            state = "recifrado en curso"
        else:
            state = f"recifrado al día desde {row['finished_at']:%Y-%m-%d %H:%M} UTC"
            if row["failed"]:
                state += f", {row['failed']} sin poder descifrar"
        print(f"{table:<13} {detail}  ({state})")
        others += sum(total for key, total in counts.items() if key != active)
    if others:
        print(f"{others} datos usan otra clave: todavía no retirar las claves anteriores.")
        return 1
    print("todo usa la clave activa: las anteriores se pueden retirar de DATA_ENCRYPTION_KEYS.")
    return 0


def _print_report(report: IngestReport) -> None:
    print(f"entradas: {report.entries}  fragmentos: {report.chunks}")
    if report.skipped_reason:
        print(f"sin cambios en el índice: {report.skipped_reason}")
    if report.version:
        print(f"versión activa: {report.version}")
    for chunk in report.preview:
        print("-" * 72)
        print(chunk.content[:600])


async def ingest_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, role="ingest")
    options = IngestOptions(
        faq_dir=args.faq_dir,
        urls_file=args.urls_file,
        from_web=args.from_web,
        skip_local=args.skip_local,
        if_empty=args.if_empty,
        if_changed=args.if_changed,
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        delay_seconds=args.delay,
    )
    if args.dry_run:
        _print_report(await run_ingest(options, None))
        return 0
    pool = await _pool(settings, "ingest")
    embedder = OpenAICompatibleEmbedder(
        base_url=settings.embeddings_base_url,
        model=settings.embeddings_model,
        api_key=settings.embeddings_api_key.get_secret_value(),
        dimensions=settings.embedding_dimensions,
        timeout=max(60.0, settings.embeddings_timeout_seconds),
    )
    try:
        await migrate(pool, embedding_dimensions=settings.embedding_dimensions)
        indexer = KnowledgeIndexer(pool, embedder, dimensions=settings.embedding_dimensions, batch_size=args.batch_size)
        if args.rollback:
            version = await indexer.rollback()
            print(f"versión activa: {version}" if version else "no hay una versión anterior a la cual volver")
            return 0 if version else 1
        _print_report(await run_ingest(options, indexer))
    finally:
        await embedder.aclose()
        await pool.close()
    return 0
