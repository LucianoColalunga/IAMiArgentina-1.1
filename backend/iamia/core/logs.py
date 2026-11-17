"""Logs estructurados en JSON (una línea por evento), listos para Loki/Elasticsearch.

Reglas que se respetan en todo el código:
- nunca se registra el texto de los mensajes ni direcciones (teléfonos, correos, IDs de usuario);
- los identificadores de personas se registran solo seudonimizados;
- las URLs no se registran con query string (pueden traer tokens de verificación).

El contexto de la tarea actual (request_id, canal, conversación) se propaga con contextvars,
así cada línea de log de un mensaje queda correlacionada sin pasarlo a mano.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("iamia_log_context", default={})  # noqa: B039

# Bibliotecas que en INFO registran URLs completas (Telegram lleva el token del bot en la ruta).
_NOISY_LOGGERS = ("httpx", "httpcore", "aiormq", "aio_pika", "asyncio", "uvicorn.access", "pamqp")


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Agrega campos a todas las líneas de log emitidas dentro del bloque."""
    current = _CONTEXT.get()
    token = _CONTEXT.set({**current, **{key: value for key, value in fields.items() if value is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


class JsonFormatter(logging.Formatter):
    def __init__(self, role: str) -> None:
        super().__init__()
        self._role = role

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "role": self._role,
        }
        payload.update(_CONTEXT.get())
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", role: str = "app") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(role))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
