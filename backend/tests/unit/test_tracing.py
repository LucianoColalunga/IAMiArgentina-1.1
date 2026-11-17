"""Trazas de OpenTelemetry: el id de traza es el trace_id del mensaje y los spans no llevan datos personales."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from iamia.api.middleware import RequestContextMiddleware
from iamia.core.tracing import child_span, root_span, setup_tracing, trace_id_from

_EXPORTER = InMemorySpanExporter()
# El proveedor global se configura una sola vez por proceso; el resto de las pruebas no lo usa.
setup_tracing("pruebas", "", exporter=_EXPORTER)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def _by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


def test_trace_ids_come_from_the_message() -> None:
    assert trace_id_from("f8fd96a4224340acb8a8b7e7a1c63411") == 0xF8FD96A4224340ACB8A8B7E7A1C63411
    assert trace_id_from("0" * 32) is None
    assert trace_id_from("no-es-hexadecimal-de-32-caracter") is None
    assert trace_id_from("") is None


def test_spans_of_a_message_share_its_trace_id(spans: InMemorySpanExporter) -> None:
    trace_id = uuid.uuid4().hex
    # Lo que haría la API y después el worker, en procesos distintos: dos raíces, la misma traza.
    with root_span("POST /webhooks/{channel}", trace_id, kind=SpanKind.SERVER):
        pass
    with root_span("inbound.process", trace_id, kind=SpanKind.CONSUMER, **{"iamia.channel": "telegram"}):
        with child_span("ai.answer"):
            pass
    finished = _by_name(spans)
    assert {span.context.trace_id for span in finished.values()} == {int(trace_id, 16)}
    assert finished["ai.answer"].parent is not None
    assert finished["ai.answer"].parent.span_id == finished["inbound.process"].context.span_id
    assert finished["inbound.process"].parent is None  # raíz propia, no hija del span de la API
    assert finished["inbound.process"].attributes["iamia.channel"] == "telegram"


def test_errors_keep_only_the_type(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="DNI 30123456"):
        with root_span("inbound.process", uuid.uuid4().hex):
            raise ValueError("DNI 30123456")  # un mensaje de error podría tener datos personales
    (span,) = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR and span.status.description == "ValueError"
    assert span.events == ()  # sin la excepción registrada como evento (traería el mensaje)


def test_none_attributes_are_dropped(spans: InMemorySpanExporter) -> None:
    with root_span("outbound.deliver", uuid.uuid4().hex, **{"iamia.channel": "sms", "iamia.retries": None}):
        pass
    (span,) = spans.get_finished_spans()
    assert dict(span.attributes) == {"iamia.channel": "sms"}


async def test_http_requests_are_traced_by_route_except_probes(spans: InMemorySpanExporter) -> None:
    async def hello(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/v1/items/{item}", hello), Route("/health/live", hello)])
    transport = httpx.ASGITransport(app=RequestContextMiddleware(app))
    request_id = uuid.uuid4().hex
    async with httpx.AsyncClient(transport=transport, base_url="http://prueba") as client:
        assert (await client.get("/v1/items/123", headers={"X-Request-ID": request_id})).status_code == 200
        assert (await client.get("/health/live")).status_code == 200
    (span,) = spans.get_finished_spans()
    assert span.name == "GET /v1/items/{item}"  # la plantilla, no la URL con el id
    assert span.kind is SpanKind.SERVER and span.context.trace_id == int(request_id, 16)
    assert span.attributes["http.response.status_code"] == 200
