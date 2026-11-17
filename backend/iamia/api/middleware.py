"""Middleware HTTP: identificador de pedido, métricas por ruta, límite de cuerpo y cabeceras de seguridad."""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

from fastapi import HTTPException
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from iamia.core.logs import log_context
from iamia.core.metrics import HTTP_REQUESTS, HTTP_SECONDS
from iamia.core.tracing import root_span, set_attributes

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

# La consola y el chat web cargan sus propios archivos y nada externo.
_PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
# Sondas y scrapes: sin span (serían la mayoría de las trazas y no dicen nada).
_UNTRACED = ("/metrics", "/healthz", "/readyz", "/health/")


_TOO_LARGE = "El cuerpo del pedido es demasiado grande."


class PayloadTooLarge(HTTPException):
    """Cuerpo enviado en partes (sin Content-Length) que supera el máximo.

    Es una HTTPException para que FastAPI no la convierta en un 400 genérico cuando aparece
    mientras lee el JSON de un endpoint: así la respuesta es 413 en todos los casos.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=_TOO_LARGE)


class RequestContextMiddleware:
    """ASGI puro (sin BaseHTTPMiddleware) para no romper el streaming ni sumar latencia."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = 1_048_576, hsts: bool = False) -> None:
        self._app = app
        self._max_body = max_body_bytes
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_body:
            await self._reject(send, request_id)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body:
                    raise PayloadTooLarge
            return message

        status_holder = {"status": 500}
        path = scope.get("path", "")
        is_page = path.startswith(("/chat", "/console"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                extra = [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", (_PAGE_CSP if is_page else _API_CSP).encode()),
                ]
                if self._hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                existing = {key.lower() for key, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in extra if h[0] not in existing]
            await send(message)

        started = time.perf_counter()
        method = scope.get("method", "")
        traced = not path.startswith(_UNTRACED)
        span_scope = (
            root_span(f"{method} {path}", request_id, kind=SpanKind.SERVER, **{"http.request.method": method})
            if traced
            else contextlib.nullcontext()
        )
        with span_scope as span:
            try:
                with log_context(request_id=request_id):
                    await self._app(scope, limited_receive, send_wrapper)
            except PayloadTooLarge:
                await self._reject(send, request_id)
                status_holder["status"] = 413
            finally:
                route = scope.get("route")
                endpoint = scope.get("endpoint")
                template = getattr(route, "path", None) or getattr(endpoint, "__name__", None) or "unmatched"
                status = status_holder["status"]
                HTTP_REQUESTS.labels(template, method, str(status)).inc()
                HTTP_SECONDS.labels(template).observe(time.perf_counter() - started)
                if span is not None:
                    # El nombre definitivo usa la plantilla de la ruta, no la URL con ids.
                    span.update_name(f"{method} {template}")
                    set_attributes(span, **{"http.route": template, "http.response.status_code": status})
                    if status >= 500:
                        span.set_status(Status(StatusCode.ERROR))

    @staticmethod
    async def _reject(send: Send, request_id: str) -> None:
        """413 antes de leer el cuerpo (Content-Length declarado demasiado grande)."""
        body = json.dumps({"detail": _TOO_LARGE}, ensure_ascii=False).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"x-request-id", request_id.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", _API_CSP.encode()),
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
