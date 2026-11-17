"""Ingesta completa: fuentes -> limpieza -> fragmentos -> índice nuevo -> activación."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from iamia.rag.ingest.chunking import Chunk, build_chunks, digest
from iamia.rag.ingest.extract import clean_entries, extract_sections
from iamia.rag.ingest.indexer import KnowledgeIndexer
from iamia.rag.ingest.sources import FaqEntry, PoliteFetcher, load_local_faqs, read_urls

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestOptions:
    faq_dir: Path
    urls_file: Path
    from_web: bool = False
    skip_local: bool = False
    if_empty: bool = False
    if_changed: bool = False
    dry_run: bool = False
    chunk_size: int = 1000
    chunk_overlap: int = 150
    delay_seconds: float = 1.0


@dataclass(frozen=True)
class IngestReport:
    entries: int
    chunks: int
    version: str | None
    skipped_reason: str | None = None
    preview: tuple[Chunk, ...] = ()


async def collect(options: IngestOptions) -> list[FaqEntry]:
    entries: list[FaqEntry] = [] if options.skip_local else load_local_faqs(options.faq_dir)
    if options.from_web:
        fetcher = PoliteFetcher(delay_seconds=options.delay_seconds)
        try:
            for url in read_urls(options.urls_file):
                html = await fetcher.get(url)
                if html is not None:
                    sections = extract_sections(html, url)
                    logger.info("page_extracted", extra={"ctx": {"url": url, "sections": len(sections)}})
                    entries.extend(sections)
        finally:
            await fetcher.aclose()
    return clean_entries(entries)


async def run_ingest(options: IngestOptions, indexer: KnowledgeIndexer | None) -> IngestReport:
    if options.if_empty and indexer is not None and await indexer.active() is not None:
        return IngestReport(0, 0, None, "ya hay una base de conocimiento activa")
    entries = await collect(options)
    chunks = build_chunks(entries, options.chunk_size, options.chunk_overlap)
    if options.dry_run or indexer is None:
        return IngestReport(len(entries), len(chunks), None, "dry-run", tuple(chunks[:5]))
    content_digest = digest(chunks)
    active = await indexer.active()
    if options.if_changed and active is not None and active.get("source_digest") == content_digest:
        return IngestReport(len(entries), len(chunks), str(active["version"]), "el contenido no cambió")
    version = await indexer.build(chunks, source_digest=content_digest)
    await indexer.activate(version)
    return IngestReport(len(entries), len(chunks), version)
