"""Traducción de errores del dominio a respuestas HTTP (sin filtrar detalles internos)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from iamia.ai.prompts import BUSY_MESSAGE, UNAVAILABLE_MESSAGE
from iamia.core.errors import (
    ChannelDisabled,
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
    QuotaExceeded,
    ServiceBusy,
    ServiceUnavailable,
    Unauthorized,
    WebhookAuthError,
    WebhookPayloadError,
)
from iamia.core.transient import TRANSIENT_ERRORS

logger = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store"}
DEPENDENCY_RETRY_AFTER = "5"


def _json(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers={**_NO_STORE, **(headers or {})})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(InvalidRequest)
    async def invalid(_: Request, exc: InvalidRequest) -> JSONResponse:
        return _json(400, exc.user_message)

    @app.exception_handler(RequestValidationError)
    async def validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _json(422, "El pedido no tiene el formato esperado.")

    @app.exception_handler(Unauthorized)
    async def unauthorized(_: Request, exc: Unauthorized) -> JSONResponse:
        return _json(401, "No autorizado.", {"WWW-Authenticate": "Bearer"})

    @app.exception_handler(WebhookAuthError)
    async def webhook_auth(_: Request, exc: WebhookAuthError) -> JSONResponse:
        return _json(401, "Firma inválida.")

    @app.exception_handler(WebhookPayloadError)
    async def webhook_payload(_: Request, exc: WebhookPayloadError) -> JSONResponse:
        return _json(400, "Contenido inválido.")

    @app.exception_handler(Forbidden)
    async def forbidden(_: Request, exc: Forbidden) -> JSONResponse:
        return _json(403, "No tenés permiso para esta acción.")

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return _json(404, str(exc) or "No encontrado.")

    @app.exception_handler(ChannelDisabled)
    async def channel_disabled(_: Request, exc: ChannelDisabled) -> JSONResponse:
        return _json(404, "No encontrado.")

    @app.exception_handler(Conflict)
    async def conflict(_: Request, exc: Conflict) -> JSONResponse:
        return _json(409, str(exc))

    @app.exception_handler(QuotaExceeded)
    async def quota(_: Request, exc: QuotaExceeded) -> JSONResponse:
        return _json(429, exc.user_message, {"Retry-After": str(exc.retry_after)})

    @app.exception_handler(ServiceBusy)
    async def busy(_: Request, exc: ServiceBusy) -> JSONResponse:
        return _json(503, BUSY_MESSAGE, {"Retry-After": str(int(exc.retry_after))})

    @app.exception_handler(ServiceUnavailable)
    async def unavailable(_: Request, exc: ServiceUnavailable) -> JSONResponse:
        return _json(503, UNAVAILABLE_MESSAGE, {"Retry-After": str(int(exc.retry_after))})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _json(exc.status_code, str(exc.detail), dict(exc.headers or {}))

    async def dependency_down(request: Request, exc: Exception) -> JSONResponse:
        # PostgreSQL o Redis reiniciándose: 503 con espera y sin la traza completa. Las plataformas
        # reintentan el webhook y el mensaje entra cuando la dependencia vuelve.
        logger.warning("dependency_unavailable", extra={"ctx": {"path": request.url.path, "error": type(exc).__name__}})
        return _json(503, UNAVAILABLE_MESSAGE, {"Retry-After": DEPENDENCY_RETRY_AFTER})

    for error in TRANSIENT_ERRORS:
        app.add_exception_handler(error, dependency_down)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra={"ctx": {"path": request.url.path}})
        return _json(500, UNAVAILABLE_MESSAGE)
