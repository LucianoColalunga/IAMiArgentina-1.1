"""Trazas de punta a punta: un mensaje deja una sola traza repartida en la API, el worker y el dispatcher.

Los tres procesos exportan sus spans por OTLP al simulador de plataformas (hace de colector). El
id de traza es el X-Request-ID del webhook, el mismo trace_id que llevan los logs del mensaje.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

from conftest import Channels, Platforms, unique_number, wait_for

pytestmark = pytest.mark.skipif(
    not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"), reason="trazas desactivadas en este entorno"
)

QUESTION = "¿Qué hago si perdí el DNI y tengo que viajar?"
EXPECTED = {
    ("iamia-api", "POST /webhooks/{channel}"),
    ("iamia-worker", "inbound.process"),
    ("iamia-worker", "ai.answer"),
    ("iamia-dispatcher", "outbound.deliver"),
    ("iamia-dispatcher", "channel.send"),
}


def test_a_message_is_one_trace_across_api_worker_and_dispatcher(channels: Channels, platforms: Platforms) -> None:
    trace_id = uuid.uuid4().hex
    chat_id = unique_number()
    assert channels.telegram(chat_id, QUESTION, request_id=trace_id).status_code == 200
    platforms.replies("telegram", str(chat_id))

    def complete() -> list[dict[str, Any]] | None:
        spans = platforms.spans(trace_id)
        return spans if EXPECTED <= {(span["service"], span["name"]) for span in spans} else None

    # Cada proceso exporta en lotes cada ~5 s.
    spans = wait_for(complete, timeout=40, interval=1, what="spans de la API, el worker y el dispatcher")
    by_name = {span["name"]: span for span in spans}

    # Cada proceso abre su propia raíz con el mismo id de traza; adentro, los spans se anidan.
    for root in ("POST /webhooks/{channel}", "inbound.process", "outbound.deliver"):
        assert by_name[root]["parent_span_id"] == "", root
    assert by_name["ai.answer"]["parent_span_id"] == by_name["inbound.process"]["span_id"]
    assert by_name["channel.send"]["parent_span_id"] == by_name["outbound.deliver"]["span_id"]
    assert by_name["POST /webhooks/{channel}"]["kind"] == "SPAN_KIND_SERVER"
    assert by_name["POST /webhooks/{channel}"]["attributes"]["http.response.status_code"] == 200
    assert by_name["inbound.process"]["attributes"]["iamia.channel"] == "telegram"
    assert by_name["channel.send"]["kind"] == "SPAN_KIND_CLIENT"
    assert all(span["status"] != "STATUS_CODE_ERROR" for span in spans)

    # Ni el texto ni la dirección de la persona viajan en las trazas.
    exported = json.dumps(spans, ensure_ascii=False)
    assert str(chat_id) not in exported
    assert "perdí el DNI" not in exported and "viajar" not in exported
