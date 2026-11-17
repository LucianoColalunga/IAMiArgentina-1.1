"""Extracción de secciones de páginas oficiales y limpieza de entradas."""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from iamia.core.text import clean_text, fold, stable_hash
from iamia.rag.ingest.sources import FaqEntry

logger = logging.getLogger(__name__)

MIN_ANSWER_CHARS = 30

# Navegación y pie de página que no aportan conocimiento (comparado sin tildes).
_BOILERPLATE = [
    re.compile(pattern)
    for pattern in (
        r"^¿?te sirvio esta informacion\??$",
        r"^compartir( en .*)?$",
        r"^(facebook|twitter|x|whatsapp|linkedin|telegram|imprimir|email)$",
        r"^volver( arriba| al inicio)?$",
        r"^ir al contenido( principal)?$",
        r"^(inicio|menu|buscar)$",
        r"^(si|no)$",
        r"este sitio (web )?utiliza cookies",
        r"^los contenidos de argentina\.gob\.ar",
        r"creative commons",
    )
]
_HEADINGS = {"h2", "h3", "h4", "h5", "h6"}
_BLOCKS = ["h2", "h3", "h4", "h5", "h6", "p", "li", "dt", "dd", "td", "th", "blockquote"]
_REMOVE = [
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "svg",
    "button",
    "template",
]


def is_boilerplate(line: str) -> bool:
    folded = fold(line)
    return any(pattern.search(folded) for pattern in _BOILERPLATE)


def is_table_of_contents(element: Any) -> bool:
    """Ítems de índice ("1. ¿Qué es?" -> #01): enlaces internos que no aportan contenido."""
    text = element.get_text(" ", strip=True)
    anchors = element.find_all("a", href=lambda href: bool(href) and href.startswith("#"))
    anchor_chars = sum(len(a.get_text(" ", strip=True)) for a in anchors)
    return bool(text) and anchor_chars >= 0.8 * len(text)


def extract_sections(html: str, url: str) -> list[FaqEntry]:
    """Convierte una página oficial en secciones (título h2-h6 -> contenido)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_REMOVE):
        tag.decompose()
    for selector in (".breadcrumb", "[role=navigation]", ".sr-only", ".visually-hidden", ".social-share"):
        for element in soup.select(selector):
            element.decompose()
    h1 = soup.find("h1")
    if h1:
        page_title = clean_text(h1.get_text(" ", strip=True))
    elif soup.title:
        page_title = clean_text(soup.title.get_text(" ", strip=True))
    else:
        page_title = url
    root = soup.find("main") or soup.find("article") or soup.body or soup

    sections: list[FaqEntry] = []
    heading = page_title
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(FaqEntry(question=heading, answer=body, url=url, category=page_title, source="web"))
        buffer.clear()

    for element in root.find_all(_BLOCKS):
        if element.name in _HEADINGS:
            flush()
            heading = clean_text(element.get_text(" ", strip=True)) or heading
            continue
        if element.find_parent(["p", "li", "dd", "td", "th", "blockquote"]) is not None:
            continue  # el texto ya se incluyó con el elemento contenedor
        if is_table_of_contents(element):
            continue
        text = clean_text(element.get_text(" ", strip=True))
        if not text or is_boilerplate(text):
            continue
        buffer.append(f"- {text}" if element.name == "li" else text)
    flush()
    return sections


def clean_entries(entries: list[FaqEntry]) -> list[FaqEntry]:
    cleaned: list[FaqEntry] = []
    seen: set[str] = set()
    for entry in entries:
        question = clean_text(entry.question)
        lines = [line for line in clean_text(entry.answer).split("\n") if not is_boilerplate(line)]
        answer = "\n".join(lines).strip()
        if len(answer) < MIN_ANSWER_CHARS:
            continue
        url = entry.url.strip() if entry.url.strip().startswith("https://") else ""
        fingerprint = stable_hash(fold(question), fold(answer))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        cleaned.append(FaqEntry(question, answer, url, clean_text(entry.category), entry.source))
    logger.info("entries_cleaned", extra={"ctx": {"kept": len(cleaned), "total": len(entries)}})
    return cleaned
