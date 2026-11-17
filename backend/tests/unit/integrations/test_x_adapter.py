"""X, mensajes directos (iamia/integrations/x/adapter.py)."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import httpx
import pytest

from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    FailureKind,
    MessageKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    WebhookBatch,
)
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.x import adapter as x_adapter
from iamia.integrations.x.adapter import XConfig, XSender, XWebhook, build_x_adapter
from iamia.integrations.x.tokens import StaticAccessToken

from .support import NOW, webhook_request, x_signature_header

ACCOUNT = "1000"
USER = "2000"
SECRET = "x-secret"
CONFIG = XConfig(account_id=ACCOUNT, webhook_secret=SECRET)


def v2_event(**overrides) -> dict:
    """Evento de la API v2 (MessageCreate)."""
    event = {
        "id": "1800000000000000001",
        "event_type": "MessageCreate",
        "text": "Hola, ¿cómo renuevo el pasaporte?",
        "sender_id": USER,
        "dm_conversation_id": f"{ACCOUNT}-{USER}",
        "created_at": "2026-11-16T12:00:00.000Z",
    }
    return {**event, **overrides}


def v11_event(message_data: dict | None = None, *, sender: str = USER, **overrides) -> dict:
    """Evento del formato v1.1 (message_create), usado por la Account Activity API."""
    event = {
        "type": "message_create",
        "id": "1800000000000000002",
        "created_timestamp": str(int(NOW.timestamp() * 1000)),
        "message_create": {
            "target": {"recipient_id": ACCOUNT},
            "sender_id": sender,
            "message_data": {"text": "Hola", "entities": {}} if message_data is None else message_data,
        },
    }
    return {**event, **overrides}


def xaa(*events, user_id: str = ACCOUNT, event_type: str = "dm.received") -> dict:
    payload = {"direct_message_events": list(events)}
    return {"data": {"event_type": event_type, "filter": {"user_id": user_id}, "tag": "iamia-dm", "payload": payload}}


def aaa(*events, for_user_id: str = ACCOUNT) -> dict:
    return {"for_user_id": for_user_id, "direct_message_events": list(events)}


def parse(payload: dict) -> WebhookBatch:
    return XWebhook(CONFIG).parse(webhook_request(payload))


# ------------------------------------------------------------------ CRC y firma
def test_crc_responde_base64_del_hmac_sha256():
    reply = XWebhook(CONFIG).challenge(webhook_request(method="GET", query={"crc_token": "crc-123"}))
    assert reply is not None
    assert reply.media_type == "application/json"
    assert reply.status_code == 200
    assert json.loads(reply.body) == {"response_token": x_signature_header(SECRET, b"crc-123")}


def test_crc_acepta_hasta_256_caracteres():
    token = "c" * 256
    reply = XWebhook(CONFIG).challenge(webhook_request(method="GET", query={"crc_token": token}))
    assert reply is not None
    assert json.loads(reply.body)["response_token"] == x_signature_header(SECRET, token.encode())


@pytest.mark.parametrize(
    ("config", "query"),
    [
        (CONFIG, {}),
        (CONFIG, {"crc_token": ""}),
        (CONFIG, {"crc_token": "c" * 257}),
        (XConfig(account_id=ACCOUNT, webhook_secret=""), {"crc_token": "crc-123"}),
    ],
    ids=["sin-token", "token-vacio", "token-largo", "sin-secreto"],
)
def test_crc_invalido(config, query):
    assert XWebhook(config).challenge(webhook_request(method="GET", query=query)) is None


BODY = json.dumps(xaa(v2_event())).encode()


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Twitter-Webhooks-Signature-OAuth2": x_signature_header(SECRET, BODY)},
        {"X-Twitter-Webhooks-Signature": x_signature_header(SECRET, BODY)},
        {
            "X-Twitter-Webhooks-Signature-OAuth2": x_signature_header("otro", BODY),
            "X-Twitter-Webhooks-Signature": x_signature_header(SECRET, BODY),
        },
    ],
    ids=["oauth2", "heredada", "una-de-las-dos"],
)
def test_firma_valida_en_cualquiera_de_los_dos_headers(headers):
    XWebhook(CONFIG).authenticate(webhook_request(BODY, headers=headers))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Twitter-Webhooks-Signature-OAuth2": x_signature_header("otro", BODY)},
        {"X-Twitter-Webhooks-Signature": x_signature_header(SECRET, BODY + b" ")},
    ],
    ids=["sin-firma", "otro-secreto", "otro-cuerpo"],
)
def test_firma_invalida(headers):
    with pytest.raises(WebhookAuthError):
        XWebhook(CONFIG).authenticate(webhook_request(BODY, headers=headers))


def test_sin_secreto_configurado_rechaza_todo():
    headers = {"X-Twitter-Webhooks-Signature": x_signature_header("", BODY)}
    with pytest.raises(WebhookAuthError):
        XWebhook(XConfig(account_id=ACCOUNT, webhook_secret="")).authenticate(webhook_request(BODY, headers=headers))


# ------------------------------------------------------------------ webhook XAA
def test_xaa_dm_received_v2():
    (message,) = parse(xaa(v2_event())).messages
    assert message.channel is Channel.X
    assert message.provider_message_id == "1800000000000000001"
    assert message.sender_id == USER
    assert message.account_id == ACCOUNT
    assert message.kind is MessageKind.TEXT
    assert message.text == "Hola, ¿cómo renuevo el pasaporte?"
    assert message.sent_at == NOW
    assert message.thread_ref == f"{ACCOUNT}-{USER}"
    assert message.extra == {}


def test_xaa_con_formato_v11_dentro_del_sobre():
    (message,) = parse(xaa(v11_event())).messages
    assert (message.provider_message_id, message.text, message.sent_at) == ("1800000000000000002", "Hola", NOW)


@pytest.mark.parametrize(
    "payload",
    [xaa(v2_event(), user_id="3000"), xaa(v2_event(), user_id=""), xaa(v2_event(), event_type="dm.sent")],
    ids=["otra-cuenta", "sin-usuario", "otro-evento"],
)
def test_xaa_de_otra_cuenta_o_evento_se_ignora(payload):
    assert parse(payload) == WebhookBatch(ignored=1)


def test_xaa_con_imagen_sin_texto():
    (message,) = parse(xaa(v2_event(text="", attachments={"media_keys": ["3_180000"]}))).messages
    assert message.kind is MessageKind.IMAGE
    assert message.attachments == (Attachment(MessageKind.IMAGE),)
    assert message.text is None


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-11-16T12:00:00", NOW),
        ("2026-11-16T09:00:00-03:00", NOW),
        ("ayer", NOW + timedelta(hours=1)),
        (None, NOW + timedelta(hours=1)),
    ],
)
def test_xaa_fechas(monkeypatch, created_at, expected):
    monkeypatch.setattr(x_adapter, "utcnow", lambda: NOW + timedelta(hours=1))
    assert parse(xaa(v2_event(created_at=created_at))).messages[0].sent_at == expected


def test_eventos_propios_incompletos_o_desconocidos_se_ignoran():
    batch = parse(
        xaa(
            v2_event(),
            v2_event(id="1800000000000000009", sender_id=ACCOUNT),  # eco del propio envío
            v2_event(id=""),
            v2_event(sender_id=""),
            {"event_type": "ParticipantsJoin", "id": "1"},
            "evento inválido",
        )
    )
    assert len(batch.messages) == 1
    assert batch.ignored == 5


# ------------------------------------------------------------------ webhook Account Activity (heredado)
def test_aaa_mensaje_de_texto():
    (message,) = parse(aaa(v11_event())).messages
    assert (message.sender_id, message.kind, message.text) == (USER, MessageKind.TEXT, "Hola")
    assert message.sent_at == NOW
    assert message.thread_ref is None


def test_aaa_respuesta_rapida():
    data = {"text": "Turnos", "quick_reply_response": {"type": "options", "metadata": "MENU_TURNOS"}}
    (message,) = parse(aaa(v11_event(data))).messages
    assert message.kind is MessageKind.INTERACTIVE
    assert message.extra == {"payload": "MENU_TURNOS"}


def test_aaa_adjunto():
    data = {"text": "", "attachment": {"type": "media", "media": {"id": 1}}}
    (message,) = parse(aaa(v11_event(data))).messages
    assert (message.kind, message.attachments) == (MessageKind.IMAGE, (Attachment(MessageKind.IMAGE),))


def test_aaa_sin_texto_ni_adjunto():
    assert parse(aaa(v11_event({}))).messages[0].kind is MessageKind.UNSUPPORTED


def test_aaa_fecha_no_numerica_usa_la_hora_actual(monkeypatch):
    monkeypatch.setattr(x_adapter, "utcnow", lambda: NOW + timedelta(hours=1))
    assert parse(aaa(v11_event(created_timestamp="ayer"))).messages[0].sent_at == NOW + timedelta(hours=1)


def test_aaa_de_otra_cuenta_se_ignora():
    assert parse(aaa(v11_event(), for_user_id="3000")) == WebhookBatch(ignored=1)


def test_aaa_eco_propio_se_ignora():
    assert parse(aaa(v11_event(sender=ACCOUNT))) == WebhookBatch(ignored=1)


def test_payload_sin_mensajes_directos_se_ignora():
    assert parse({"for_user_id": ACCOUNT, "tweet_create_events": [{"id": "1"}]}) == WebhookBatch(ignored=1)


@pytest.mark.parametrize("body", [b"no es json", b"[]", b'"texto"'])
def test_payload_invalido(body):
    with pytest.raises(WebhookPayloadError):
        XWebhook(CONFIG).parse(webhook_request(body))


# ------------------------------------------------------------------ envío
class StubTokens:
    def __init__(self, current: str = "test-token", refreshable: bool = True) -> None:
        self.token = current
        self.refreshable = refreshable
        self.invalidated: list[str] = []
        self.closed = False

    async def access_token(self) -> str:
        return self.token

    async def invalidate(self, token: str) -> bool:
        self.invalidated.append(token)
        return self.refreshable

    async def aclose(self) -> None:
        self.closed = True


RECIPIENT = Recipient(Channel.X, USER, ACCOUNT)
DM_CREATED = httpx.Response(201, json={"data": {"dm_conversation_id": f"{ACCOUNT}-{USER}", "dm_event_id": "dm-1"}})


@pytest.fixture
def x_sender(fake_api):
    def build(*replies, tokens=None):
        api = fake_api(*replies)
        tokens = tokens or StubTokens()
        return XSender(CONFIG, tokens, api.http(Channel.X)), api, tokens

    return build


async def send_error(sender: XSender) -> DeliveryError:
    with pytest.raises(DeliveryError) as caught:
        await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    return caught.value


async def test_envio_de_mensaje_directo(x_sender):
    sender, api, _ = x_sender(DM_CREATED)
    assert await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW)) == SendResult("dm-1")
    assert api.last.method == "POST"
    assert str(api.last.url) == f"https://api.x.com/2/dm_conversations/with/{USER}/messages"
    assert api.last.headers["authorization"] == "Bearer test-token"
    assert api.json() == {"text": "Hola"}


async def test_401_con_token_renovable_es_reintentable(x_sender):
    sender, _, tokens = x_sender(httpx.Response(401, json={"title": "Unauthorized", "status": 401}))
    error = await send_error(sender)
    assert (error.kind, error.code) == (FailureKind.RETRYABLE, "401")
    assert tokens.invalidated == ["test-token"]


async def test_401_con_token_fijo_es_de_autenticacion(x_sender):
    sender, _, _ = x_sender(httpx.Response(401, json={}), tokens=StaticAccessToken("test-token"))
    assert (await send_error(sender)).kind is FailureKind.AUTH


async def test_429_usa_x_rate_limit_reset(x_sender):
    reset = str(int(time.time()) + 120)
    sender, _, _ = x_sender(
        httpx.Response(429, json={"title": "Too Many Requests"}, headers={"x-rate-limit-reset": reset})
    )
    error = await send_error(sender)
    assert error.kind is FailureKind.RATE_LIMITED
    assert error.retry_after == pytest.approx(120, abs=3)


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [
        ({"retry-after": "30", "x-rate-limit-reset": str(int(time.time()) + 600)}, 30.0),
        ({"x-rate-limit-reset": str(int(time.time()) - 60)}, 1.0),  # el reinicio ya pasó
        ({}, 60.0),
    ],
    ids=["retry-after", "reinicio-pasado", "sin-headers"],
)
async def test_429_espera(x_sender, headers, retry_after):
    sender, _, _ = x_sender(httpx.Response(429, json={}, headers=headers))
    assert (await send_error(sender)).retry_after == retry_after


@pytest.mark.parametrize(
    ("body", "kind", "code"),
    [
        (
            {"type": "https://api.twitter.com/2/problems/client-forbidden", "title": "Forbidden"},
            FailureKind.AUTH,
            "403:client-forbidden",
        ),
        (
            {"type": "https://api.twitter.com/2/problems/unsupported-authentication"},
            FailureKind.AUTH,
            "403:unsupported-authentication",
        ),
        (
            {"type": "https://api.twitter.com/2/problems/not-authorized-for-resource"},
            FailureKind.AUTH,
            "403:not-authorized-for-resource",
        ),
        (
            {"detail": "You are not allowed to send a message to this user.", "title": "Forbidden"},
            FailureKind.PERMANENT,
            "403",
        ),
    ],
    ids=["cliente", "autenticacion", "recurso", "usuario-no-acepta-dm"],
)
async def test_403(x_sender, body, kind, code):
    sender, _, _ = x_sender(httpx.Response(403, json=body))
    error = await send_error(sender)
    assert (error.kind, error.code) == (kind, code)


async def test_detalle_del_error(x_sender):
    sender, _, _ = x_sender(httpx.Response(403, json={"detail": "x" * 300, "title": "Forbidden"}))
    assert str(await send_error(sender)) == "x" * 200
    sender, _, _ = x_sender(httpx.Response(400, json={"title": "Invalid Request"}))
    assert str(await send_error(sender)) == "Invalid Request"
    sender, _, _ = x_sender(httpx.Response(503, text="Service Unavailable"))
    assert str(await send_error(sender)) == "error de X"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, FailureKind.PERMANENT),
        (404, FailureKind.PERMANENT),
        (500, FailureKind.RETRYABLE),
        (503, FailureKind.RETRYABLE),
    ],
)
async def test_otros_errores(x_sender, status, kind):
    sender, _, _ = x_sender(httpx.Response(status, json={}))
    error = await send_error(sender)
    assert (error.kind, error.code) == (kind, str(status))


async def test_aclose_cierra_tambien_el_proveedor_de_tokens(x_sender):
    sender, _, tokens = x_sender(DM_CREATED)
    await sender.aclose()
    assert tokens.closed
    static_sender, _, _ = x_sender(DM_CREATED, tokens=StaticAccessToken("test-token"))
    await static_sender.aclose()  # sin aclose en el proveedor: no falla


def test_adaptador_completo():
    adapter = build_x_adapter(CONFIG, StaticAccessToken("test-token"))
    assert adapter.channel is Channel.X
    assert adapter.accepts_webhooks
    assert adapter.typing is None
    assert adapter.capabilities.max_text_chars == 10_000
