"""Utilidades compartidas por las pruebas de los adaptadores de canales."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

import httpx

from iamia.channels.contracts import WebhookRequest
from iamia.channels.models import Channel
from iamia.integrations.http import ProviderHttp

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

Reply = httpx.Response | Exception | Callable[[httpx.Request], Any]


class FakeApi:
    """API falsa sobre httpx.MockTransport: guarda cada pedido y contesta en orden.

    La última respuesta preparada se repite para los pedidos que sobren.
    """

    def __init__(self, *replies: Reply) -> None:
        self.requests: list[httpx.Request] = []
        self._replies = list(replies)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> Any:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(f"pedido inesperado: {request.method} {request.url.path}")
        reply = self._replies[0] if len(self._replies) == 1 else self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):  # copia nueva: una respuesta de httpx no se reutiliza
            return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)
        return reply(request)

    def http(self, channel: Channel) -> ProviderHttp:
        return ProviderHttp(channel, client=self.client)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def json(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def form(self, index: int = -1) -> dict[str, str]:
        return dict(parse_qsl(self.requests[index].content.decode("utf-8"), keep_blank_values=True))


def webhook_request(
    body: bytes | str | Mapping[str, Any] = b"",
    *,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    method: str = "POST",
    url: str = "https://iamia.example.test/webhooks/canal",
    received_at: float = 0.0,
) -> WebhookRequest:
    if isinstance(body, Mapping):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return WebhookRequest(
        method=method,
        url=url,
        headers={name.lower(): value for name, value in (headers or {}).items()},
        query=dict(query or {}),
        body=body,
        received_at=received_at,
    )


def meta_signature_header(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def x_signature_header(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode()


def twilio_signature_header(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """HMAC-SHA1 de la URL más los parámetros ordenados por nombre, como documenta Twilio."""
    payload = url + "".join(f"{name}{value}" for name, value in sorted(params.items()))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def assert_split(original: str, parts: list[str]) -> None:
    """Las partes son tramos consecutivos del original: en los cortes solo se pierde espacio en blanco."""
    position = 0
    for part in parts:
        index = original.find(part, position)
        assert index >= 0, f"parte alterada o fuera de orden: {part[:40]!r}"
        assert not original[position:index].strip(), "se perdió texto entre dos partes"
        position = index + len(part)
    assert not original[position:].strip(), "se perdió el final del texto"
