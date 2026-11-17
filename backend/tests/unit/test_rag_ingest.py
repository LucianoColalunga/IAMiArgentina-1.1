"""Ingesta de la base de conocimiento: fuentes, extracción, limpieza y fragmentos."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
import pytest

from iamia.rag.ingest.chunking import build_chunks, digest, split_recursive
from iamia.rag.ingest.extract import clean_entries, extract_sections
from iamia.rag.ingest.pipeline import IngestOptions, run_ingest
from iamia.rag.ingest.sources import FaqEntry, PoliteFetcher, is_official_url, load_local_faqs, read_urls

REPO_DATA = Path(__file__).resolve().parents[3] / "data"


def test_split_recursive_respects_size_and_overlap() -> None:
    text = " ".join(f"Oración número {i} sobre el trámite." for i in range(60))
    pieces = split_recursive(text, 200, 40)
    assert len(pieces) > 1 and all(len(piece) <= 200 for piece in pieces)
    for previous, following in itertools.pairwise(pieces):
        assert following[:10] in previous  # el solapamiento repite el final del fragmento anterior


def test_split_recursive_edge_cases() -> None:
    assert split_recursive("corto", 100, 10) == ["corto"]
    assert split_recursive("   ", 100, 10) == []
    assert all(len(piece) <= 100 for piece in split_recursive("a" * 250, 100, 20))


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (100, 100), (100, -1)])
def test_split_recursive_rejects_invalid_arguments(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        split_recursive("texto", size, overlap)


def test_chunks_are_self_contained_and_versioned() -> None:
    entry = FaqEntry(
        "¿Cómo saco el DNI?", "Con turno en el Registro Civil. " * 80, "https://www.argentina.gob.ar/dni", "DNI"
    )
    chunks = build_chunks([entry], chunk_size=500, chunk_overlap=50)
    assert len(chunks) > 1
    assert all(c.content.startswith("Trámite: DNI\nPregunta: ¿Cómo saco el DNI?\nRespuesta: ") for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)
    assert digest(chunks) == digest(build_chunks([entry], chunk_size=500, chunk_overlap=50))
    changed = FaqEntry(entry.question, entry.answer + " Cambió.", entry.url, entry.category)
    assert digest(chunks) != digest(build_chunks([changed], chunk_size=500, chunk_overlap=50))


PAGE = """<html><head><title>DNI</title></head><body>
<header><nav>Inicio Menú</nav></header>
<main>
<h1>Documento Nacional de Identidad</h1>
<ul class="indice"><li><a href="#requisitos">Requisitos</a></li></ul>
<h2 id="requisitos">Requisitos</h2>
<p>Presentar la partida de nacimiento y sacar turno previo en el Registro Civil.</p>
<ul><li>Turno previo</li><li>Partida de nacimiento</li></ul>
<p>¿Te sirvió esta información?</p>
<h2>Costo</h2>
<p>El valor se actualiza periódicamente; consultá el monto vigente en el sitio oficial.</p>
<script>alert('x')</script>
</main>
<footer>Los contenidos de argentina.gob.ar están licenciados bajo Creative Commons</footer>
</body></html>"""


def test_extract_sections_keeps_content_and_drops_navigation() -> None:
    sections = extract_sections(PAGE, "https://www.argentina.gob.ar/dni")
    assert [section.question for section in sections] == ["Requisitos", "Costo"]
    first = sections[0]
    assert first.category == "Documento Nacional de Identidad" and first.source == "web"
    assert "partida de nacimiento" in first.answer and "- Turno previo" in first.answer
    assert "sirvió" not in first.answer
    joined = " ".join(section.answer for section in sections)
    assert "alert" not in joined and "Creative Commons" not in joined and "Inicio" not in joined


def test_clean_entries_dedupes_and_filters() -> None:
    answer = "Se tramita con turno previo en el Registro Civil de tu localidad."
    entries = [
        FaqEntry("¿Cómo saco el DNI?", answer, "https://www.argentina.gob.ar/dni", "DNI"),
        FaqEntry("¿cómo saco el dni?", answer.upper(), "https://www.argentina.gob.ar/dni", "DNI"),
        FaqEntry("¿Corta?", "Muy corta.", "https://www.argentina.gob.ar/x"),
        FaqEntry("¿Sin https?", answer + " Otra.", "http://www.argentina.gob.ar/y"),
    ]
    cleaned = clean_entries(entries)
    assert len(cleaned) == 2  # duplicado y respuesta demasiado corta afuera
    assert cleaned[1].url == ""  # solo se conservan enlaces https


def test_load_local_faqs_and_urls(tmp_path: Path) -> None:
    faqs = {
        "faqs": [
            {
                "pregunta": "¿Qué es?",
                "respuesta": "Es un trámite.",
                "url": "https://www.argentina.gob.ar/a",
                "tramite": "A",
            }
        ]
    }
    (tmp_path / "faqs.json").write_text(json.dumps(faqs), encoding="utf-8")
    (tmp_path / "guia.md").write_text(
        "# Guía de licencias\nFuente: https://www.argentina.gob.ar/licencia\n\nTexto de la guía.", encoding="utf-8"
    )
    entries = load_local_faqs(tmp_path)
    assert {entry.question for entry in entries} == {"¿Qué es?", "Guía de licencias"}
    guide = next(entry for entry in entries if entry.source == "guia.md")
    assert guide.url == "https://www.argentina.gob.ar/licencia"
    assert load_local_faqs(tmp_path / "no-existe") == []

    urls = tmp_path / "urls.txt"
    urls.write_text("# comentario\n\nhttps://www.argentina.gob.ar/dni\n", encoding="utf-8")
    assert read_urls(urls) == ["https://www.argentina.gob.ar/dni"]
    assert read_urls(tmp_path / "no-existe.txt") == []


@pytest.mark.parametrize(
    ("url", "official"),
    [
        ("https://www.argentina.gob.ar/dni", True),
        ("https://gob.ar", True),
        ("http://www.argentina.gob.ar/dni", False),
        ("https://argentina.gob.ar.evil.com/", False),
        ("https://evilgob.ar/", False),
    ],
)
def test_only_official_https_urls(url: str, official: bool) -> None:
    assert is_official_url(url) is official


async def test_polite_fetcher_respects_robots_domains_and_redirects() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /privado\n")
        if request.url.path == "/redirige":
            return httpx.Response(302, headers={"Location": "https://ejemplo.com/"})
        if request.url.path == "/interna":
            return httpx.Response(302, headers={"Location": "http://10.0.0.5/admin"})
        if request.url.path == "/otra-oficial":
            return httpx.Response(301, headers={"Location": "https://www.argentina.gob.ar/dni"})
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = PoliteFetcher(delay_seconds=0, client=client)
    try:
        assert await fetcher.get("https://www.argentina.gob.ar/dni") == "<html>ok</html>"
        assert await fetcher.get("https://www.argentina.gob.ar/privado/x") is None  # robots.txt
        assert await fetcher.get("https://ejemplo.com/") is None  # dominio no oficial
        assert await fetcher.get("https://www.argentina.gob.ar/redirige") is None  # redirección afuera
        assert await fetcher.get("https://www.argentina.gob.ar/interna") is None  # redirección a la red interna
        assert await fetcher.get("https://www.argentina.gob.ar/otra-oficial") == "<html>ok</html>"
    finally:
        await fetcher.aclose()
    # Las redirecciones afuera se cortan antes de pedir nada.
    assert not [url for url in requested if not url.startswith("https://www.argentina.gob.ar/")]


async def test_dry_run_with_the_bundled_knowledge_base() -> None:
    options = IngestOptions(faq_dir=REPO_DATA / "faqs", urls_file=REPO_DATA / "sources.txt", dry_run=True)
    report = await run_ingest(options, None)
    assert report.entries > 10 and report.chunks >= report.entries
    assert report.version is None and report.skipped_reason == "dry-run" and len(report.preview) == 5
    assert all(chunk.url.startswith("https://") for chunk in report.preview)
