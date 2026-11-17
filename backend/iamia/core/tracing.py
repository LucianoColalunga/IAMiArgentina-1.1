"""Trazas distribuidas con OpenTelemetry (opcionales).

Con OTEL_EXPORTER_OTLP_ENDPOINT vacío no se configura nada: la API de OpenTelemetry sin SDK no
hace nada y los spans no cuestan. Con un colector configurado (compose.observability.yml trae
uno) cada proceso exporta sus spans por OTLP/HTTP.

Cada mensaje usa como id de traza el mismo trace_id que va en los logs y en el trabajo de la
cola. Así la API, el worker y el dispatcher suman sus spans a una sola traza aunque corran en
procesos distintos, y desde una línea de log en Loki se salta a la traza en Tempo.

Los spans no llevan textos, direcciones ni mensajes de excepciones: solo canal, ids internos,
resultados y el tipo de error.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

AttributeValue = str | bool | int | float

_HEX_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_forced_trace_id: ContextVar[int | None] = ContextVar("iamia_trace_id", default=None)
_tracer = trace.get_tracer("iamia")


def trace_id_from(value: str) -> int | None:
    """El trace_id de los logs como id de traza de OpenTelemetry (32 hex, distinto de cero)."""
    if value and _HEX_TRACE_ID.match(value):
        return int(value, 16) or None
    return None


def setup_tracing(role: str, endpoint: str, *, sample_ratio: float = 1.0, exporter: SpanExporter | None = None) -> bool:
    """Configura el SDK si hay colector (o un exportador de pruebas). Devuelve si quedó activo."""
    if not endpoint and exporter is None:
        return False
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    class MessageTraceIds(RandomIdGenerator):
        def generate_trace_id(self) -> int:
            return _forced_trace_id.get() or super().generate_trace_id()

    provider = TracerProvider(
        resource=Resource.create({"service.name": f"iamia-{role}", "service.namespace": "iamia"}),
        # La decisión depende solo del id de traza: todos los procesos muestrean los mismos mensajes.
        sampler=TraceIdRatioBased(min(1.0, max(0.0, sample_ratio))),
        id_generator=MessageTraceIds(),
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        otlp = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(otlp))
    trace.set_tracer_provider(provider)
    return True


def _clean(attributes: dict[str, AttributeValue | None]) -> dict[str, AttributeValue]:
    return {key: value for key, value in attributes.items() if value is not None}


@contextlib.contextmanager
def root_span(
    name: str, trace_id: str, *, kind: SpanKind = SpanKind.INTERNAL, **attributes: AttributeValue | None
) -> Iterator[Span]:
    """Span raíz de un pedido o de un trabajo, con el trace_id del mensaje como id de traza."""
    token = _forced_trace_id.set(trace_id_from(trace_id))
    try:
        with _tracer.start_as_current_span(
            name,
            context=otel_context.Context(),
            kind=kind,
            attributes=_clean(attributes),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
    finally:
        _forced_trace_id.reset(token)


@contextlib.contextmanager
def child_span(name: str, *, kind: SpanKind = SpanKind.INTERNAL, **attributes: AttributeValue | None) -> Iterator[Span]:
    with _tracer.start_as_current_span(
        name, kind=kind, attributes=_clean(attributes), record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def set_attributes(span: Span, **attributes: AttributeValue | None) -> None:
    if span.is_recording():
        span.set_attributes(_clean(attributes))
