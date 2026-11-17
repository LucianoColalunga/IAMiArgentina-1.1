"""Utilidades de formato compartidas por los adaptadores."""

from __future__ import annotations

import math
import unicodedata

from iamia.channels.models import OutboundContent, RenderedPart
from iamia.core.text import fold, truncate

_BOUNDARIES = ("\n\n", "\n", ". ", "; ", ", ", " ")


def split_text(text: str, limit: int) -> list[str]:
    """Divide un texto largo en partes que respetan el límite del canal.

    Corta preferentemente entre párrafos, después entre líneas, oraciones y palabras.
    """
    text = text.strip()
    if limit <= 0:
        raise ValueError("el límite debe ser positivo")
    parts: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = 0
        for separator in _BOUNDARIES:
            index = window.rfind(separator)
            if index >= limit // 2:
                cut = index + (1 if separator in (". ", "; ", ", ") else 0)
                break
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def split_text_bytes(text: str, max_bytes: int, *, char_limit: int | None = None) -> list[str]:
    """Como split_text, pero el límite es en bytes UTF-8 (Instagram limita el texto a 1000 bytes).

    Si una parte excede el límite se vuelve a partir con un límite de caracteres menor; termina
    porque el límite baja en cada vuelta y un carácter ocupa como máximo 4 bytes.
    """
    if max_bytes < 4:
        raise ValueError("el límite en bytes debe ser al menos 4")
    limit = char_limit or max_bytes
    parts: list[str] = []
    for part in split_text(text, limit):
        size = len(part.encode("utf-8"))
        if size <= max_bytes:
            parts.append(part)
        else:
            smaller = max(1, min(limit - 1, len(part) * max_bytes // size))
            parts.extend(split_text_bytes(part, max_bytes, char_limit=smaller))
    return parts


def compose_text(content: OutboundContent, *, include_sources: bool = True) -> str:
    """Texto final: la respuesta y, si el modelo no citó la fuente, la primera fuente oficial."""
    text = content.text.strip()
    if include_sources and content.sources and "fuente:" not in fold(text):
        text = f"{text}\n\nFuente: {content.sources[0].url}"
    return text


class PlainTextFormatter:
    """Formato por defecto: texto plano partido según el largo máximo del canal."""

    def __init__(self, max_chars: int, *, include_sources: bool = True, max_bytes: int | None = None) -> None:
        self._max_chars = max_chars
        self._max_bytes = max_bytes
        self._include_sources = include_sources

    def render(self, content: OutboundContent) -> list[RenderedPart]:
        text = compose_text(content, include_sources=self._include_sources)
        if self._max_bytes:
            parts = split_text_bytes(text, self._max_bytes, char_limit=min(self._max_chars, self._max_bytes))
        else:
            parts = split_text(text, self._max_chars)
        return [RenderedPart(part) for part in parts]


# ----------------------------------------------------------------------- SMS (GSM-7)
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = set("^{}\\[~]|€")  # ocupan dos caracteres
_REPLACEMENTS = {
    "“": '"',
    "”": '"',
    "«": '"',
    "»": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "-",
    "…": "...",
    "•": "-",
    "·": "-",
    "\t": " ",
    "‹": "<",
    "›": ">",
}


def to_gsm7(text: str) -> str:
    """Translitera a GSM-7 para que un SMS en español no pase a UCS-2 (70 caracteres por parte).

    "á", "í", "ó" y "ú" no existen en GSM-7 y se reemplazan por la vocal sin tilde; "é", "ñ",
    "ü", "¿" y "¡" sí existen y se conservan.
    """
    output: list[str] = []
    for char in text:
        if char in _GSM7 or char in _GSM7_EXTENDED:
            output.append(char)
        elif char in _REPLACEMENTS:
            output.append(_REPLACEMENTS[char])
        else:
            base = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c))
            output.append(base if base and all(c in _GSM7 for c in base) else "?")
    return "".join(output)


def gsm7_length(text: str) -> int:
    return sum(2 if char in _GSM7_EXTENDED else 1 for char in text)


def sms_segments(text: str) -> int:
    length = gsm7_length(text)
    return 1 if length <= 160 else math.ceil(length / 153)


def fit_sms(text: str, max_segments: int) -> str:
    """Recorta el texto para que entre en `max_segments` partes de SMS concatenado."""
    limit = 160 if max_segments <= 1 else 153 * max_segments
    if gsm7_length(text) <= limit:
        return text
    return truncate(text, limit - 2, ellipsis="...")
