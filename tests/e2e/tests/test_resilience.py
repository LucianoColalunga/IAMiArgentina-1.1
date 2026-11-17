"""Fallas inyectadas: la plataforma reintenta, respeta las esperas, aísla el canal caído y no pierde mensajes.

Los tiempos de los circuit breakers están acortados en compose.e2e.yml (3 fallas, 5 s abierto).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx
import pytest

from conftest import Channels, Console, MockAI, Platforms, fold, unique_number, wait_for

pytestmark = pytest.mark.resilience

QUESTION = "¿Cómo solicito y activo el DNI en el celular?"
DISPATCHER_METRICS = "http://dispatcher:9100/metrics"
_CIRCUIT = re.compile(r'^iamia_circuit_state\{name="channel:(?P<channel>[a-z]+)"\} (?P<value>[0-9.]+)$', re.MULTILINE)
CLOSED, OPEN = 0, 2


def official(text: str) -> bool:
    return fold(text).startswith("Segun la informacion oficial:")


def circuit_state(tools: httpx.Client, channel: str) -> int | None:
    metrics = tools.get(DISPATCHER_METRICS).text
    for match in _CIRCUIT.finditer(metrics):
        if match.group("channel") == channel:
            return int(float(match.group("value")))
    return None


def test_platform_errors_are_retried(channels: Channels, platforms: Platforms) -> None:
    attempts = platforms.attempts("telegram")
    platforms.fault("telegram", 500, count=1)
    chat_id = unique_number()
    channels.telegram(chat_id, QUESTION)
    (reply,) = platforms.replies("telegram", str(chat_id), timeout=45)
    assert official(reply["text"])
    assert platforms.attempts("telegram") >= attempts + 2  # el primer intento falló y se reintentó


def test_rate_limits_are_respected(channels: Channels, platforms: Platforms) -> None:
    body = {"error": {"message": "(#130429) Rate limit hit", "type": "OAuthException", "code": 130429}}
    platforms.fault("whatsapp", 429, count=1, retry_after=2, body=body)
    phone = f"54911{unique_number()}"
    started = time.monotonic()
    channels.whatsapp(phone, QUESTION)
    (reply,) = platforms.replies("whatsapp", phone, timeout=45)
    assert official(reply["text"])
    assert time.monotonic() - started >= 2  # esperó lo que pidió la plataforma antes de reintentar


def test_a_failing_channel_does_not_affect_the_others(
    channels: Channels, platforms: Platforms, tools: httpx.Client
) -> None:
    platforms.fault("messenger", 503, count=1000)
    psids = [f"24{unique_number()}" for _ in range(4)]
    for psid in psids:
        channels.messenger(psid, QUESTION)
    wait_for(lambda: circuit_state(tools, "messenger") == OPEN, timeout=40, what="circuito de Messenger abierto")

    # Con Messenger caído, WhatsApp responde igual de rápido: cada canal tiene su cola y su circuito.
    phone = f"54911{unique_number()}"
    started = time.monotonic()
    channels.whatsapp(phone, QUESTION)
    platforms.replies("whatsapp", phone, timeout=20)
    assert time.monotonic() - started < 10
    assert circuit_state(tools, "whatsapp") == CLOSED

    # Messenger vuelve: el circuito deja pasar una prueba, se cierra y salen los mensajes pendientes.
    platforms.clear_faults()
    for psid in psids:
        (reply,) = platforms.replies("messenger", psid, timeout=120)
        assert official(reply["text"])
    wait_for(lambda: circuit_state(tools, "messenger") == CLOSED, timeout=30, what="circuito de Messenger cerrado")


def test_permanent_failures_reach_the_operators(
    channels: Channels, platforms: Platforms, console: Callable[[str, str], Console]
) -> None:
    body = {"code": 21211, "message": "Invalid 'To' Phone Number", "status": 400}
    platforms.fault("sms", 400, count=1, body=body)
    phone = f"+54911{unique_number()}"
    channels.sms(phone, QUESTION)

    admin = console("admin", "admin.e2e")
    conversation = admin.find("sms", phone, "bot")

    def failed() -> dict[str, object] | None:
        items = admin.get("/problems").json()["items"]
        return next((item for item in items if item["conversation_id"] == conversation["id"]), None)

    problem = wait_for(failed, timeout=30, what="envío fallido visible en la consola")
    assert problem["status"] == "failed" and problem["error_code"] == "permanent:21211"
    assert platforms.outbox("sms", phone) == []  # no se reintentó solo: el error es permanente

    assert admin.post(f"/problems/{problem['id']}/retry").status_code == 202
    replies = platforms.replies("sms", phone, timeout=30)
    assert fold(replies[0]["text"]).startswith("Segun la informacion oficial:")


def test_model_failures_are_retried(channels: Channels, platforms: Platforms, ai: MockAI) -> None:
    calls = ai.stats()["calls"]["chat"]
    ai.control(chat_failures=1)
    chat_id = unique_number()
    channels.telegram(chat_id, "¿Cómo asocio a mis hijos a mi cuenta de Mi Argentina?")
    (reply,) = platforms.replies("telegram", str(chat_id), timeout=75)
    assert official(reply["text"]), reply["text"]
    assert ai.stats()["calls"]["chat"] >= calls + 2  # falló una vez y se volvió a intentar
