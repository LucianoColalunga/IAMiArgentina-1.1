"""Piezas comunes de Meta: errores de la Graph API, webhook y cliente (iamia/integrations/meta/common.py)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from iamia.channels.contracts import HttpReply
from iamia.channels.models import Channel, DeliveryError, FailureKind
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.meta.common import (
    GraphClient,
    MetaWebhookVerifier,
    load_payload,
    meta_error,
    regain_access_seconds,
)

from .support import meta_signature_header, webhook_request

APP_SECRET = "app-secret"
VERIFY_TOKEN = "verify-token"
BODY = b'{"object":"page","entry":[]}'


def _usage(*minutes: float) -> str:
    """Header X-Business-Use-Case-Usage con las esperas indicadas (en minutos)."""
    items = [{"type": "messenger", "call_count": 100, "estimated_time_to_regain_access": m} for m in minutes]
    return json.dumps({"123456": items})


# ------------------------------------------------------------------ regain_access_seconds
def test_regain_access_seconds_toma_la_espera_mas_larga():
    usage = {
        "123456": [{"type": "messenger", "estimated_time_to_regain_access": 2}],
        "654321": [
            {"type": "pages", "estimated_time_to_regain_access": 5},
            {"type": "instagram", "estimated_time_to_regain_access": 1},
        ],
    }
    assert regain_access_seconds({"x-business-use-case-usage": json.dumps(usage)}) == 300.0


def test_regain_access_seconds_con_headers_de_httpx():
    assert regain_access_seconds(httpx.Headers({"X-Business-Use-Case-Usage": _usage(3)})) == 180.0


@pytest.mark.parametrize("raw", [None, "", "no es json", "[1, 2]", _usage(0), json.dumps({"123456": "x"})])
def test_regain_access_seconds_sin_dato_util(raw):
    headers = {} if raw is None else {"x-business-use-case-usage": raw}
    assert regain_access_seconds(headers) is None


# ------------------------------------------------------------------ meta_error
@pytest.mark.parametrize(
    ("status", "error", "kind", "code"),
    [
        (400, {"code": 131047}, FailureKind.WINDOW_CLOSED, "131047"),
        (400, {"code": "131047"}, FailureKind.WINDOW_CLOSED, "131047"),
        (400, {"code": 10, "error_subcode": 2018278}, FailureKind.WINDOW_CLOSED, "10/2018278"),
        (400, {"code": 100, "error_subcode": "2018278"}, FailureKind.WINDOW_CLOSED, "100/2018278"),
        (401, {"code": 190, "error_subcode": 463}, FailureKind.AUTH, "190/463"),
        (403, {"code": 10}, FailureKind.AUTH, "10"),
        (400, {"code": 200}, FailureKind.AUTH, "200"),
        (400, {"code": 131031}, FailureKind.AUTH, "131031"),
        (400, {"code": 100}, FailureKind.PERMANENT, "100"),
        (400, {"code": 131026}, FailureKind.PERMANENT, "131026"),
        (400, {"code": 551}, FailureKind.PERMANENT, "551"),
        (500, {"code": 1}, FailureKind.RETRYABLE, "1"),
        (400, {"code": 131000}, FailureKind.RETRYABLE, "131000"),
        (400, {"code": 133004}, FailureKind.RETRYABLE, "133004"),
        # Código desconocido: decide el status HTTP.
        (400, {"code": 999999}, FailureKind.PERMANENT, "999999"),
        (503, {"code": 999999}, FailureKind.RETRYABLE, "999999"),
        (200, {"code": 999999}, FailureKind.RETRYABLE, "999999"),
        (429, {}, FailureKind.RATE_LIMITED, "429"),
        (401, {}, FailureKind.AUTH, "401"),
        (502, {}, FailureKind.RETRYABLE, "502"),
    ],
)
def test_meta_error_clasifica_por_codigo_y_subcodigo(status, error, kind, code):
    exc = meta_error(status, {"error": error} if error else {}, {})
    assert exc.kind is kind
    assert exc.code == code


@pytest.mark.parametrize(
    ("code", "wait"),
    [(131056, 6.0), (130429, 1.0), (131048, 600.0), (4, 60.0), (17, 60.0), (32, 60.0), (613, 60.0), (80007, 60.0)],
)
def test_meta_error_rate_limited_con_espera_por_defecto(code, wait):
    exc = meta_error(400, {"error": {"code": code}}, {})
    assert exc.kind is FailureKind.RATE_LIMITED
    assert exc.retry_after == wait


def test_meta_error_rate_limited_prefiere_los_headers():
    assert meta_error(429, {"error": {"code": 131056}}, {"retry-after": "12"}).retry_after == 12.0
    usage = {"x-business-use-case-usage": _usage(2)}
    assert meta_error(400, {"error": {"code": 80007}}, usage).retry_after == 120.0


def test_meta_error_detalle():
    error = {"code": 100, "message": "Invalid parameter", "error_user_title": "Número inválido"}
    assert str(meta_error(400, {"error": error}, {})) == "Número inválido"
    assert str(meta_error(400, {"error": {"code": 100, "message": "x" * 300}}, {})) == "x" * 200
    assert str(meta_error(500, {}, {})) == "error de la plataforma de Meta"
    assert str(meta_error(500, {"error": "texto"}, {})) == "error de la plataforma de Meta"


# ------------------------------------------------------------------ verificación del webhook
@pytest.fixture
def verifier() -> MetaWebhookVerifier:
    return MetaWebhookVerifier(app_secret=APP_SECRET, verify_token=VERIFY_TOKEN)


def _subscribe(overrides: dict[str, str] | None = None, drop: str = ""):
    query = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"}
    query.update(overrides or {})
    query.pop(drop, None)
    return webhook_request(method="GET", query=query)


def test_challenge_devuelve_el_desafio_tal_cual(verifier):
    assert verifier.challenge(_subscribe()) == HttpReply("1158201444", media_type="text/plain", status_code=200)


def test_challenge_acepta_hasta_128_caracteres(verifier):
    assert verifier.challenge(_subscribe({"hub.challenge": "a1" * 64})) == HttpReply("a1" * 64)


@pytest.mark.parametrize(
    "overrides",
    [
        {"hub.verify_token": "otro-token"},
        {"hub.verify_token": ""},
        {"hub.mode": "unsubscribe"},
        {"hub.challenge": ""},
        {"hub.challenge": "<script>alert(1)</script>"},
        {"hub.challenge": "abc-123"},
        {"hub.challenge": "abc 123"},
        {"hub.challenge": "a" * 129},
    ],
    ids=["token-incorrecto", "token-vacio", "modo", "desafio-vacio", "html", "guion", "espacio", "largo"],
)
def test_challenge_rechaza(verifier, overrides):
    assert verifier.challenge(_subscribe(overrides)) is None


@pytest.mark.parametrize("missing", ["hub.mode", "hub.verify_token", "hub.challenge"])
def test_challenge_con_parametros_faltantes(verifier, missing):
    assert verifier.challenge(_subscribe(drop=missing)) is None


def test_challenge_sin_verify_token_configurado():
    verifier = MetaWebhookVerifier(app_secret=APP_SECRET, verify_token="")
    assert verifier.challenge(_subscribe({"hub.verify_token": ""})) is None


def test_authenticate_acepta_la_firma_del_cuerpo(verifier):
    headers = {"X-Hub-Signature-256": meta_signature_header(APP_SECRET, BODY)}
    verifier.authenticate(webhook_request(BODY, headers=headers))


def test_authenticate_firma_los_bytes_tal_cual_llegaron(verifier):
    # Meta firma el JSON con unicode escapado: volver a serializarlo cambiaría la firma.
    escaped = json.dumps({"text": "ñandú"}).encode()
    header = meta_signature_header(APP_SECRET, escaped)
    verifier.authenticate(webhook_request(escaped, headers={"x-hub-signature-256": header}))
    reencoded = json.dumps({"text": "ñandú"}, ensure_ascii=False).encode()
    with pytest.raises(WebhookAuthError):
        verifier.authenticate(webhook_request(reencoded, headers={"x-hub-signature-256": header}))


@pytest.mark.parametrize(
    "headers",
    [{}, {"x-hub-signature-256": "sha256=deadbeef"}, {"x-hub-signature-256": meta_signature_header("otro", BODY)}],
    ids=["sin-firma", "firma-falsa", "otro-secreto"],
)
def test_authenticate_rechaza(verifier, headers):
    with pytest.raises(WebhookAuthError):
        verifier.authenticate(webhook_request(BODY, headers=headers))


def test_authenticate_sin_app_secret_rechaza_todo():
    verifier = MetaWebhookVerifier(app_secret="", verify_token=VERIFY_TOKEN)
    with pytest.raises(WebhookAuthError):
        verifier.authenticate(webhook_request(BODY, headers={"x-hub-signature-256": meta_signature_header("", BODY)}))


# ------------------------------------------------------------------ load_payload
def test_load_payload_del_producto_esperado():
    payload = {"object": "page", "entry": [{"id": "123456"}]}
    assert load_payload(json.dumps(payload).encode(), "page") == payload


def test_load_payload_de_otro_producto_se_ignora():
    assert load_payload(b'{"object": "instagram", "entry": []}', "page") is None


@pytest.mark.parametrize(
    "body",
    [b"no es json", b"\xff\xfe", b"[]", b'"texto"', b'{"object": "page", "entry": {}}'],
    ids=["texto", "bytes-invalidos", "lista", "string", "entry-no-lista"],
)
def test_load_payload_invalido(body):
    with pytest.raises(WebhookPayloadError):
        load_payload(body, "page")


# ------------------------------------------------------------------ GraphClient
def _graph(api, **kwargs) -> GraphClient:
    kwargs.setdefault("app_secret", APP_SECRET)
    return GraphClient(Channel.MESSENGER, access_token="test-token", http=api.http(Channel.MESSENGER), **kwargs)


async def test_graph_client_post_con_token_y_appsecret_proof(fake_api):
    api = fake_api(httpx.Response(200, json={"recipient_id": "24000000000000001", "message_id": "m_1"}))
    data = await _graph(api).post("/123456/messages", {"recipient": {"id": "24000000000000001"}})
    assert data == {"recipient_id": "24000000000000001", "message_id": "m_1"}
    request = api.last
    assert request.method == "POST"
    assert request.url.host == "graph.facebook.com"
    assert request.url.path == "/v27.0/123456/messages"
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.url.params["appsecret_proof"] == hmac.new(b"app-secret", b"test-token", hashlib.sha256).hexdigest()
    assert api.json() == {"recipient": {"id": "24000000000000001"}}


@pytest.mark.parametrize(("app_secret", "enabled"), [("", True), (APP_SECRET, False)])
async def test_graph_client_sin_appsecret_proof(fake_api, app_secret, enabled):
    api = fake_api(httpx.Response(200, json={}))
    await _graph(api, app_secret=app_secret, appsecret_proof=enabled).post("123456/messages", {})
    assert "appsecret_proof" not in api.last.url.params


async def test_graph_client_normaliza_base_y_version(fake_api):
    api = fake_api(httpx.Response(200, json={}))
    graph = _graph(api, base_url="https://graph.example.test/", api_version="/v99.0/", app_secret="")
    await graph.post("/17841400000000000/messages", {})
    assert str(api.last.url) == "https://graph.example.test/v99.0/17841400000000000/messages"


async def test_graph_client_error_en_el_cuerpo_aunque_el_status_sea_200(fake_api):
    api = fake_api(httpx.Response(200, json={"error": {"code": 131047, "message": "Re-engagement message"}}))
    with pytest.raises(DeliveryError) as caught:
        await _graph(api).post("123456/messages", {})
    assert caught.value.kind is FailureKind.WINDOW_CLOSED
    assert str(caught.value) == "Re-engagement message"


@pytest.mark.parametrize(
    ("headers", "code", "wait"),
    [
        ({"Retry-After": "9"}, 4, 9.0),
        ({"X-Business-Use-Case-Usage": _usage(4)}, 80007, 240.0),
        ({}, 131056, 6.0),
    ],
)
async def test_graph_client_lee_la_espera_de_los_headers(fake_api, headers, code, wait):
    api = fake_api(httpx.Response(429, json={"error": {"code": code}}, headers=headers))
    with pytest.raises(DeliveryError) as caught:
        await _graph(api).post("123456/messages", {})
    assert caught.value.kind is FailureKind.RATE_LIMITED
    assert caught.value.retry_after == wait
