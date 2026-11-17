"""Cliente HTTP común a los adaptadores (iamia/integrations/http.py)."""

from __future__ import annotations

from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
from prometheus_client import REGISTRY

from iamia.channels.models import Channel, DeliveryError, FailureKind
from iamia.integrations import http as http_module
from iamia.integrations.http import ProviderHttp, classify_status, parse_retry_after, safe_json

from .support import NOW

TOKEN_URL = "https://api.telegram.org/bot123456:test-token/sendMessage"  # credencial en la ruta, como en Telegram


@pytest.mark.parametrize(("value", "expected"), [("30", 30.0), (" 7 ", 7.0), ("0", 0.0)])
def test_parse_retry_after_en_segundos(value, expected):
    assert parse_retry_after(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "pronto", "1.5", "-3", "2026-11-16T12:00:00Z"])
def test_parse_retry_after_invalido(value):
    assert parse_retry_after(value) is None


def test_parse_retry_after_con_fecha_http(monkeypatch):
    monkeypatch.setattr(http_module, "utcnow", lambda: NOW)
    assert parse_retry_after(format_datetime(NOW + timedelta(seconds=90), usegmt=True)) == 90.0
    assert parse_retry_after(format_datetime(NOW - timedelta(minutes=5), usegmt=True)) == 0.0  # fecha pasada


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, None),
        (201, None),
        (304, None),
        (400, FailureKind.PERMANENT),
        (404, FailureKind.PERMANENT),
        (409, FailureKind.PERMANENT),
        (422, FailureKind.PERMANENT),
        (401, FailureKind.AUTH),
        (403, FailureKind.AUTH),
        (408, FailureKind.RETRYABLE),
        (429, FailureKind.RATE_LIMITED),
        (500, FailureKind.RETRYABLE),
        (502, FailureKind.RETRYABLE),
        (503, FailureKind.RETRYABLE),
    ],
)
def test_classify_status(status, expected):
    assert classify_status(status) is expected


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"ok": True}), {"ok": True}),
        (httpx.Response(200, json=[1, 2]), {}),
        (httpx.Response(200, json="texto"), {}),
        (httpx.Response(502, text="<html>Bad gateway</html>"), {}),
        (httpx.Response(204), {}),
        (httpx.Response(200, content=b"\xff\xfe\xfa"), {}),
    ],
    ids=["objeto", "lista", "texto-json", "html", "vacio", "bytes-invalidos"],
)
def test_safe_json(response, expected):
    assert safe_json(response) == expected


async def test_request_reenvia_el_pedido_con_el_timeout_configurado(fake_api):
    api = fake_api(httpx.Response(200, json={"ok": True}))
    http = ProviderHttp(Channel.TELEGRAM, timeout=7.5, client=api.client)
    response = await http.request("POST", "https://api.example.test/v1/send", json={"a": 1}, headers={"X-Test": "1"})
    assert response.status_code == 200
    assert api.last.method == "POST"
    assert api.last.url == "https://api.example.test/v1/send"
    assert api.last.headers["x-test"] == "1"
    assert api.json() == {"a": 1}
    assert api.last.extensions["timeout"] == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}


async def test_request_devuelve_las_respuestas_de_error_sin_lanzar(fake_api):
    # La clasificación por status la hace cada adaptador.
    http = fake_api(httpx.Response(500, json={"error": "x"})).http(Channel.SMS)
    assert (await http.request("GET", "https://api.example.test/")).status_code == 500


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (httpx.ConnectTimeout("lento"), "timeout", "timeout de la plataforma"),
        (httpx.ReadTimeout("lento"), "timeout", "timeout de la plataforma"),
        (httpx.ConnectError("caído"), "network", "error de red: ConnectError"),
        (httpx.RemoteProtocolError("cortado"), "network", "error de red: RemoteProtocolError"),
    ],
)
async def test_request_errores_de_red_son_reintentables_y_no_exponen_la_url(fake_api, error, code, detail):
    http = fake_api(error).http(Channel.TELEGRAM)
    with pytest.raises(DeliveryError) as caught:
        await http.request("POST", TOKEN_URL)
    assert caught.value.kind is FailureKind.RETRYABLE
    assert caught.value.code == code
    assert str(caught.value) == detail
    assert caught.value.__cause__ is error


def _observations(channel: str) -> float:
    return REGISTRY.get_sample_value("iamia_delivery_seconds_count", {"channel": channel}) or 0.0


async def test_request_mide_la_latencia_tambien_cuando_falla(fake_api):
    before = _observations("sms")
    http = fake_api(httpx.Response(200), httpx.ConnectError("caído")).http(Channel.SMS)
    await http.request("GET", "https://api.example.test/ok")
    with pytest.raises(DeliveryError):
        await http.request("GET", "https://api.example.test/falla")
    assert _observations("sms") == before + 2


async def test_aclose_no_cierra_un_cliente_inyectado(fake_api):
    api = fake_api(httpx.Response(200))
    await api.http(Channel.X).aclose()
    assert not api.client.is_closed


async def test_aclose_cierra_el_cliente_propio():
    http = ProviderHttp(Channel.X, timeout=1.0)
    await http.aclose()
    assert http._client.is_closed
