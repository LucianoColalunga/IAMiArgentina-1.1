"""Fuentes de la base de conocimiento: FAQs locales (JSON/Markdown) y páginas oficiales *.gob.ar."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "IAMiArgentina-Ingest/1.1 (indexador de preguntas frecuentes de sitios oficiales)"
ALLOWED_SCRAPE_DOMAINS = ("gob.ar",)


@dataclass(frozen=True)
class FaqEntry:
    question: str
    answer: str
    url: str
    category: str = ""
    source: str = "local"


def load_local_faqs(faq_dir: Path) -> list[FaqEntry]:
    """Lee FAQs en JSON (lista o {"faqs": [...]}) y documentos Markdown/TXT."""
    entries: list[FaqEntry] = []
    if not faq_dir.is_dir():
        logger.warning("faq_dir_missing", extra={"ctx": {"path": str(faq_dir)}})
        return entries
    for path in sorted(faq_dir.rglob("*")):
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("faqs", []) if isinstance(data, dict) else data
            for item in items:
                entries.append(
                    FaqEntry(
                        question=str(item.get("pregunta") or item.get("question") or ""),
                        answer=str(item.get("respuesta") or item.get("answer") or ""),
                        url=str(item.get("url") or item.get("fuente") or ""),
                        category=str(item.get("tramite") or item.get("categoria") or item.get("category") or ""),
                        source=path.name,
                    )
                )
        elif suffix in {".md", ".txt"}:
            text = path.read_text(encoding="utf-8")
            title = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
            url = re.search(r"^(?:url|fuente)\s*:\s*(https://\S+)", text, flags=re.MULTILINE | re.IGNORECASE)
            entries.append(
                FaqEntry(
                    question=title.group(1) if title else path.stem.replace("_", " "),
                    answer=text,
                    url=url.group(1) if url else "",
                    source=path.name,
                )
            )
    logger.info("local_faqs_loaded", extra={"ctx": {"entries": len(entries)}})
    return entries


def read_urls(path: Path) -> list[str]:
    if not path.is_file():
        logger.warning("urls_file_missing", extra={"ctx": {"path": str(path)}})
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def is_official_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == d or host.endswith(f".{d}") for d in ALLOWED_SCRAPE_DOMAINS)


class OutsideOfficialDomains(httpx.RequestError):
    """Un pedido (por ejemplo, una redirección) apuntaba fuera de los dominios oficiales."""


async def _only_official(request: httpx.Request) -> None:
    # Corre antes de cada pedido, redirecciones incluidas: una página oficial que redirige a otro
    # sitio o a una dirección interna no llega a generar el pedido.
    if not is_official_url(str(request.url)):
        raise OutsideOfficialDomains("fuera de los dominios oficiales", request=request)


class PoliteFetcher:
    """Descarga respetando robots.txt, solo dominios oficiales y con pausa entre pedidos."""

    def __init__(
        self, delay_seconds: float = 1.0, timeout_seconds: float = 20.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        hooks = self._client.event_hooks
        self._client.event_hooks = {**hooks, "request": [*hooks.get("request", []), _only_official]}
        self._robots: dict[str, RobotFileParser] = {}
        self._delay = delay_seconds
        self._last = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _robots_for(self, base: str) -> RobotFileParser:
        if base not in self._robots:
            parser = RobotFileParser()
            try:
                response = await self._client.get(f"{base}/robots.txt")
                if response.status_code in (401, 403):
                    parser.disallow_all = True
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            except httpx.HTTPError:
                parser.parse([])
            self._robots[base] = parser
        return self._robots[base]

    async def allowed(self, url: str) -> bool:
        if not is_official_url(url):
            return False
        parts = urlsplit(url)
        robots = await self._robots_for(f"{parts.scheme}://{parts.netloc}")
        return robots.can_fetch(USER_AGENT, url)

    async def get(self, url: str) -> str | None:
        if not await self.allowed(url):
            logger.warning("url_skipped", extra={"ctx": {"url": url}})
            return None
        wait = self._delay - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            response = await self._client.get(url)
            self._last = time.monotonic()
            response.raise_for_status()
        except OutsideOfficialDomains:
            logger.warning("url_redirected_outside", extra={"ctx": {"url": url}})
            return None
        except httpx.HTTPError as exc:
            logger.warning("url_download_failed", extra={"ctx": {"url": url, "error": type(exc).__name__}})
            return None
        return response.text
