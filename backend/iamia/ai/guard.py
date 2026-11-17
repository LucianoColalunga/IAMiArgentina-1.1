"""Validación de la salida del modelo antes de entregarla o cachearla.

Se bloquea la fuga del system prompt (canario y marcadores), se limpian ecos del prompt,
se normalizan las respuestas fijas y se eliminan HTML y enlaces fuera de los dominios oficiales.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from iamia.ai.prompts import NO_INFO_MESSAGE, REFUSAL_MESSAGE
from iamia.core.text import clean_text, fold, normalize_for_cache, truncate

_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]{0,200}>")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.IGNORECASE)
# Solo el system prompt es confidencial. Que el modelo repita la pregunta del usuario o el
# contexto (bloques del mensaje del usuario) no es una fuga: se limpia en vez de bloquear.
_LEAK_MARKERS = (
    "reglas obligatorias",
    "codigo interno",
    "responde unicamente basandote en el contexto oficial",
)
_ECHOED_BLOCK = re.compile(
    r"<\s*(pregunta_usuario|contexto_oficial|historial)\s*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_STRAY_PROMPT_TAG = re.compile(r"</?\s*(pregunta_usuario|contexto_oficial|historial)\s*>", re.IGNORECASE)
_ECHOED_INSTRUCTION = re.compile(
    r"^\s*respond[oeé]\s+la\s+consulta\s+del\s+ciudadano\b.*$", re.IGNORECASE | re.MULTILINE
)
# Algunos modelos razonadores emiten su "pensamiento" entre etiquetas: nunca se muestra.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_REFUSAL_KEY = normalize_for_cache(REFUSAL_MESSAGE)
_NO_INFO_KEY = "no encontre informacion oficial"
REMOVED_LINK = "[enlace no oficial removido]"


@dataclass(frozen=True)
class GuardResult:
    text: str
    violation: str | None = None

    @property
    def is_refusal(self) -> bool:
        return self.text == REFUSAL_MESSAGE

    @property
    def is_no_info(self) -> bool:
        return self.text == NO_INFO_MESSAGE


def _is_allowed_url(url: str, allowed_domains: list[str]) -> bool:
    candidate = url if "://" in url else f"https://{url}"
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http"):
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def _filter_url(url: str, allowed_domains: list[str]) -> str:
    trailing = ""
    while url and url[-1] in ".,;:!?":
        trailing = url[-1] + trailing
        url = url[:-1]
    return (url if _is_allowed_url(url, allowed_domains) else REMOVED_LINK) + trailing


def strip_reasoning(text: str) -> str:
    return _THINK_BLOCK.sub("", text)


def guard_output(text: str, canary: str, allowed_domains: list[str], max_chars: int = 2500) -> GuardResult:
    if canary and canary in text:
        return GuardResult(REFUSAL_MESSAGE, "canary_leak")
    text = strip_reasoning(text)
    text = _ECHOED_BLOCK.sub("", text)
    text = _STRAY_PROMPT_TAG.sub("", text)
    text = _ECHOED_INSTRUCTION.sub("", text)
    text = clean_text(text)
    if not text:
        return GuardResult(NO_INFO_MESSAGE, "empty_output")
    folded = fold(text)
    if any(marker in folded for marker in _LEAK_MARKERS):
        return GuardResult(REFUSAL_MESSAGE, "prompt_leak")

    # Si el modelo se negó o no encontró información, se devuelve el texto exacto.
    normalized = normalize_for_cache(text)
    if _REFUSAL_KEY in normalized:
        return GuardResult(REFUSAL_MESSAGE)
    # "No encontré información" seguido de una derivación con fuente oficial es una respuesta
    # útil (p. ej. "consultá el valor vigente en RENAPER"): solo se reemplaza si no cita fuente.
    if _NO_INFO_KEY in normalized and "fuente" not in normalized:
        return GuardResult(NO_INFO_MESSAGE)

    text = _HTML_TAG.sub("", text)
    text = _URL.sub(lambda match: _filter_url(match.group(0), allowed_domains), text)
    return GuardResult(truncate(text, max_chars).strip())


def guard_rewrite(text: str, max_chars: int = 300) -> str | None:
    """Valida la pregunta reescrita: una línea corta, sin etiquetas ni restos del prompt."""
    text = clean_text(strip_reasoning(text)).strip().strip('"«»“”').strip()
    if not text or "\n" in text or len(text) > max_chars:
        return None
    if _STRAY_PROMPT_TAG.search(text) or any(marker in fold(text) for marker in _LEAK_MARKERS):
        return None
    return text
