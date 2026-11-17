"""WhatsApp Cloud API (iamia/integrations/whatsapp/adapter.py)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    DeliveryState,
    FailureKind,
    IdentityChange,
    MessageKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.whatsapp.adapter import (
    TEMPLATE_PARAMETER_MAX_CHARS,
    WhatsAppConfig,
    WhatsAppSender,
    WhatsAppWebhook,
    addressing,
    build_whatsapp_adapter,
    is_bsuid,
)

from .support import NOW, meta_signature_header, webhook_request

PHONE_ID = "123456"
PHONE = "5491122334455"
BSUID = "AR.13491208655302741918"
TS = str(int(NOW.timestamp()))
CONFIG = WhatsAppConfig(
    phone_number_id=PHONE_ID, access_token="test-token", app_secret="app-secret", verify_token="verify-token"
)


def wa_payload(*, messages=(), statuses=(), contacts=(), phone_number_id=PHONE_ID, field="messages") -> dict:
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "5491100000000", "phone_number_id": phone_number_id},
    }
    for key, items in (("contacts", contacts), ("messages", messages), ("statuses", statuses)):
        if items:
            value[key] = list(items)
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA-1", "changes": [{"field": field, "value": value}]}],
    }


def wa_message(message_type: str, content=None, **fields) -> dict:
    raw = {"from": PHONE, "id": "wamid.IN1", "timestamp": TS, "type": message_type, **fields}
    if content is not None:
        raw[message_type] = content
    return raw


def parse(payload: dict) -> WebhookBatch:
    return WhatsAppWebhook(CONFIG).parse(webhook_request(payload))


# ------------------------------------------------------------------ identidad (BSUID)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (BSUID, True),
        ("US.1", True),
        ("AR.ENT.abc123XYZ", True),
        ("AR." + "a" * 128, True),
        ("AR." + "a" * 129, False),
        (PHONE, False),
        ("+5491122334455", False),
        ("ar.123", False),
        ("ARG.123", False),
        ("AR.", False),
        ("AR.abc-123", False),
    ],
)
def test_is_bsuid(value, expected):
    assert is_bsuid(value) is expected


def test_addressing_recipient_para_bsuid_y_to_para_telefono():
    assert addressing(BSUID) == {"recipient": BSUID}
    assert addressing(PHONE) == {"to": PHONE}


# ------------------------------------------------------------------ webhook
def test_firma_y_desafio_de_suscripcion():
    webhook = WhatsAppWebhook(CONFIG)
    body = json.dumps(wa_payload()).encode()
    webhook.authenticate(
        webhook_request(body, headers={"X-Hub-Signature-256": meta_signature_header("app-secret", body)})
    )
    with pytest.raises(WebhookAuthError):
        webhook.authenticate(
            webhook_request(body, headers={"X-Hub-Signature-256": meta_signature_header("otro", body)})
        )
    query = {"hub.mode": "subscribe", "hub.verify_token": "verify-token", "hub.challenge": "987654321"}
    reply = webhook.challenge(webhook_request(method="GET", query=query))
    assert reply is not None and reply.body == "987654321"


def test_mensaje_de_texto():
    raw = wa_message(
        "text", {"body": "Hola, ¿qué necesito para el DNI?"}, context={"from": PHONE_ID, "id": "wamid.PREV"}
    )
    batch = parse(wa_payload(messages=[raw]))
    assert (batch.statuses, batch.identity_changes, batch.ignored) == ((), (), 0)
    (message,) = batch.messages
    assert message.channel is Channel.WHATSAPP
    assert message.provider_message_id == "wamid.IN1"
    assert message.sender_id == PHONE
    assert message.account_id == PHONE_ID
    assert message.kind is MessageKind.TEXT
    assert message.text == "Hola, ¿qué necesito para el DNI?"
    assert message.sent_at == NOW
    assert message.reply_to == "wamid.PREV"
    assert message.extra == {}


def test_contacts_mapea_wa_id_a_user_id():
    contacts = [{"profile": {"name": "Ana"}, "wa_id": PHONE, "user_id": BSUID}]
    (message,) = parse(wa_payload(messages=[wa_message("text", {"body": "Hola"})], contacts=contacts)).messages
    assert message.sender_id == BSUID


def test_from_user_id_tiene_prioridad_sobre_contacts():
    contacts = [{"wa_id": PHONE, "user_id": BSUID}]
    raw = wa_message("text", {"body": "Hola"}, from_user_id="AR.99887766")
    assert parse(wa_payload(messages=[raw], contacts=contacts)).messages[0].sender_id == "AR.99887766"


def test_usuario_con_nombre_de_usuario_sin_telefono():
    raw = {"from_user_id": BSUID, "id": "wamid.IN1", "timestamp": TS, "type": "text", "text": {"body": "Hola"}}
    assert parse(wa_payload(messages=[raw])).messages[0].sender_id == BSUID


@pytest.mark.parametrize(
    "raw",
    [wa_message("text", {"body": "Hola"}, id=""), {"id": "wamid.IN1", "timestamp": TS, "type": "text"}],
    ids=["sin-id", "sin-remitente"],
)
def test_mensaje_sin_id_o_sin_remitente_es_invalido(raw):
    with pytest.raises(WebhookPayloadError):
        parse(wa_payload(messages=[raw]))


@pytest.mark.parametrize(
    ("raw", "kind", "text", "payload"),
    [
        (
            wa_message(
                "interactive", {"type": "button_reply", "button_reply": {"id": "TURNO", "title": "Sacar turno"}}
            ),
            MessageKind.INTERACTIVE,
            "Sacar turno",
            "TURNO",
        ),
        (
            wa_message("interactive", {"type": "list_reply", "list_reply": {"id": "DNI", "title": "Documento"}}),
            MessageKind.INTERACTIVE,
            "Documento",
            "DNI",
        ),
        (
            wa_message("interactive", {"type": "button_reply", "button_reply": {"id": "SOLO_ID"}}),
            MessageKind.INTERACTIVE,
            "SOLO_ID",
            "SOLO_ID",
        ),
        (
            wa_message("button", {"text": "Sí, continuar", "payload": "CONTINUAR"}),
            MessageKind.INTERACTIVE,
            "Sí, continuar",
            "CONTINUAR",
        ),
        (wa_message("request_welcome"), MessageKind.INTERACTIVE, "/start", ""),
        (wa_message("location", {"latitude": -34.6, "longitude": -58.4}), MessageKind.LOCATION, None, ""),
        (wa_message("contacts", [{"name": {"formatted_name": "Ana"}}]), MessageKind.CONTACT, None, ""),
        (wa_message("unsupported", errors=[{"code": 131051}]), MessageKind.UNSUPPORTED, None, ""),
        (wa_message("order", {"catalog_id": "1"}), MessageKind.UNSUPPORTED, None, ""),
    ],
    ids=[
        "boton",
        "lista",
        "boton-sin-titulo",
        "boton-de-plantilla",
        "request-welcome",
        "ubicacion",
        "contactos",
        "no-soportado",
        "pedido",
    ],
)
def test_tipos_de_mensaje(raw, kind, text, payload):
    (message,) = parse(wa_payload(messages=[raw])).messages
    assert message.kind is kind
    assert message.text == text
    assert message.extra == ({"payload": payload} if payload else {})


@pytest.mark.parametrize(
    ("media_type", "kind"),
    [
        ("image", MessageKind.IMAGE),
        ("document", MessageKind.DOCUMENT),
        ("audio", MessageKind.AUDIO),
        ("video", MessageKind.VIDEO),
        ("sticker", MessageKind.STICKER),
    ],
)
def test_medios_registran_solo_metadatos(media_type, kind):
    media = {
        "id": "media-1",
        "mime_type": "application/pdf",
        "sha256": "abc",
        "caption": "Mi DNI",
        "filename": "dni.pdf",
    }
    (message,) = parse(wa_payload(messages=[wa_message(media_type, media)])).messages
    assert message.kind is kind
    assert message.text == "Mi DNI"
    assert message.attachments == (
        Attachment(kind, mime_type="application/pdf", file_name="dni.pdf", provider_ref="media-1"),
    )


def test_medio_sin_epigrafe_no_tiene_texto():
    (message,) = parse(
        wa_payload(messages=[wa_message("image", {"id": "media-1", "mime_type": "image/jpeg"})])
    ).messages
    assert message.text is None


def test_reacciones_se_ignoran():
    raw = wa_message("reaction", {"message_id": "wamid.PREV", "emoji": "👍"})
    assert parse(wa_payload(messages=[raw])) == WebhookBatch(ignored=1)


def test_cambio_de_bsuid():
    system = {
        "body": "Cambió el usuario",
        "type": "user_changed_user_id",
        "previous_user_id": "AR.viejo1",
        "user_id": "AR.nuevo1",
    }
    raw = {"from_user_id": "AR.nuevo1", "id": "wamid.S1", "timestamp": TS, "type": "system", "system": system}
    batch = parse(wa_payload(messages=[raw]))
    assert batch.identity_changes == (IdentityChange(Channel.WHATSAPP, "AR.viejo1", "AR.nuevo1", NOW),)
    assert (batch.messages, batch.ignored) == ((), 0)


def test_cambio_de_bsuid_con_los_campos_en_el_mensaje():
    raw = {
        "from_user_id": "AR.nuevo1",
        "previous_user_id": "AR.viejo1",
        "id": "wamid.S1",
        "timestamp": TS,
        "type": "system",
        "system": {"type": "user_changed_user_id"},
    }
    (change,) = parse(wa_payload(messages=[raw])).identity_changes
    assert (change.previous_sender_id, change.new_sender_id) == ("AR.viejo1", "AR.nuevo1")


def test_cambio_de_numero():
    system = {"body": "Ana cambió de número", "new_wa_id": "5491199998888", "type": "user_changed_number"}
    raw = {"from": PHONE, "id": "wamid.S1", "timestamp": TS, "type": "system", "system": system}
    batch = parse(wa_payload(messages=[raw]))
    assert batch.identity_changes == (IdentityChange(Channel.WHATSAPP, PHONE, "5491199998888", NOW),)


@pytest.mark.parametrize(
    "system",
    [
        {"type": "user_changed_user_id", "previous_user_id": "AR.mismo", "user_id": "AR.mismo"},
        {"type": "user_changed_user_id"},
        {"type": "otro_aviso", "previous_user_id": "AR.viejo1", "user_id": "AR.nuevo1"},
        None,
    ],
    ids=["mismo-id", "sin-anterior", "tipo-desconocido", "sin-system"],
)
def test_mensajes_de_sistema_sin_cambio_util_se_ignoran(system):
    raw = {"from": PHONE, "id": "wamid.S1", "timestamp": TS, "type": "system", "system": system}
    assert parse(wa_payload(messages=[raw])) == WebhookBatch(ignored=1)


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("sent", DeliveryState.SENT),
        ("delivered", DeliveryState.DELIVERED),
        ("read", DeliveryState.READ),
        ("played", DeliveryState.READ),
        ("failed", DeliveryState.FAILED),
    ],
)
def test_estados_de_entrega(status, state):
    raw = {"id": "wamid.OUT1", "status": status, "timestamp": TS, "recipient_id": PHONE}
    assert parse(wa_payload(statuses=[raw])).statuses == (StatusUpdate(Channel.WHATSAPP, "wamid.OUT1", state, NOW),)


def test_estado_fallido_con_error():
    error = {"code": 131047, "title": "Re-engagement message", "message": "More than 24 hours have passed"}
    raw = {"id": "wamid.OUT1", "status": "failed", "timestamp": TS, "errors": [error]}
    (update,) = parse(wa_payload(statuses=[raw])).statuses
    assert (update.state, update.error_code, update.error_title) == (
        DeliveryState.FAILED,
        "131047",
        "Re-engagement message",
    )


@pytest.mark.parametrize("raw", [{"id": "wamid.OUT1", "status": "deleted"}, {"status": "read"}, "texto"])
def test_estados_desconocidos_se_ignoran(raw):
    assert parse(wa_payload(statuses=[raw])) == WebhookBatch(ignored=1)


def test_otro_numero_de_la_cuenta_se_ignora():
    payload = wa_payload(messages=[wa_message("text", {"body": "Hola"})], phone_number_id="999999")
    assert parse(payload) == WebhookBatch(ignored=1)


def test_cambios_que_no_son_mensajes_se_ignoran():
    payload = wa_payload(messages=[wa_message("text", {"body": "Hola"})], field="message_template_status_update")
    assert parse(payload) == WebhookBatch(ignored=1)


def test_webhook_de_otro_producto_se_ignora():
    assert parse({"object": "page", "entry": []}) == WebhookBatch(ignored=1)


def test_lote_mixto():
    payload = wa_payload(
        messages=[
            wa_message("text", {"body": "Hola"}),
            wa_message("reaction", {"message_id": "wamid.PREV", "emoji": "👍"}, id="wamid.IN2"),
        ],
        statuses=[{"id": "wamid.OUT1", "status": "delivered", "timestamp": TS}, {"id": "wamid.OUT2", "status": "x"}],
    )
    batch = parse(payload)
    assert (len(batch.messages), len(batch.statuses), batch.ignored) == (1, 1, 2)


# ------------------------------------------------------------------ envío
OK = httpx.Response(
    200,
    json={
        "messaging_product": "whatsapp",
        "contacts": [{"input": PHONE, "wa_id": PHONE}],
        "messages": [{"id": "wamid.OUT"}],
    },
)
WINDOW_ERROR = httpx.Response(
    400,
    json={"error": {"message": "(#131047) Re-engagement message", "type": "OAuthException", "code": 131047}},
)


def recipient(address: str = PHONE, elapsed: timedelta | None = timedelta(hours=1)) -> Recipient:
    return Recipient(Channel.WHATSAPP, address, PHONE_ID, last_inbound_at=None if elapsed is None else NOW - elapsed)


@pytest.fixture
def whatsapp(fake_api):
    def build(*replies, **overrides):
        api = fake_api(*replies)
        return build_whatsapp_adapter(replace(CONFIG, **overrides), api.http(Channel.WHATSAPP)).sender, api

    return build


async def test_texto_dentro_de_la_ventana(whatsapp):
    sender, api = whatsapp(OK)
    assert await sender.send(recipient(), RenderedPart("Hola"), SendContext(now=NOW)) == SendResult("wamid.OUT")
    assert api.last.url.path == f"/v27.0/{PHONE_ID}/messages"
    assert api.last.headers["authorization"] == "Bearer test-token"
    assert "appsecret_proof" in api.last.url.params
    assert api.json() == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": PHONE,
        "type": "text",
        "text": {"preview_url": False, "body": "Hola"},
    }


async def test_bsuid_se_direcciona_con_recipient(whatsapp):
    sender, api = whatsapp(OK)
    await sender.send(recipient(BSUID), RenderedPart("Hola"), SendContext(now=NOW))
    body = api.json()
    assert body["recipient"] == BSUID
    assert "to" not in body


@pytest.mark.parametrize(
    ("elapsed", "is_open"),
    [
        (timedelta(hours=1), True),
        (timedelta(hours=23, minutes=54), True),
        (timedelta(hours=23, minutes=56), False),  # margen de seguridad de 5 minutos
        (timedelta(days=2), False),
        (None, False),
    ],
)
def test_ventana_de_servicio(elapsed, is_open):
    assert WhatsAppSender.window_open(recipient(elapsed=elapsed), SendContext(now=NOW)) is is_open


async def test_fuera_de_la_ventana_usa_la_plantilla(whatsapp):
    sender, api = whatsapp(OK, reengagement_template="recontacto_tramites")
    text = "Hola,\n\n¿seguís   necesitando\tayuda con el trámite?"
    result = await sender.send(recipient(elapsed=timedelta(hours=30)), RenderedPart(text), SendContext(now=NOW))
    assert result == SendResult("wamid.OUT")
    assert len(api.requests) == 1
    assert api.json() == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": PHONE,
        "type": "template",
        "template": {
            "name": "recontacto_tramites",
            "language": {"code": "es_AR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": "Hola, ¿seguís necesitando ayuda con el trámite?"}],
                }
            ],
        },
    }


async def test_parametro_de_plantilla_hasta_1000_caracteres(whatsapp):
    sender, api = whatsapp(OK, reengagement_template="recontacto", template_language="es")
    text = "\n".join(["Paso a paso del trámite del DNI digital."] * 60)
    await sender.send(recipient(elapsed=None), RenderedPart(text), SendContext(now=NOW))
    template = api.json()["template"]
    parameter = template["components"][0]["parameters"][0]["text"]
    assert template["language"] == {"code": "es"}
    assert len(parameter) <= TEMPLATE_PARAMETER_MAX_CHARS
    assert "\n" not in parameter and "  " not in parameter
    assert parameter.endswith("…")
    assert " ".join(text.split()).startswith(parameter.removesuffix("…"))


async def test_fuera_de_la_ventana_sin_plantilla_falla_sin_llamar_a_la_api(whatsapp):
    sender, api = whatsapp(OK)
    with pytest.raises(DeliveryError) as caught:
        await sender.send(recipient(elapsed=timedelta(hours=30)), RenderedPart("Hola"), SendContext(now=NOW))
    assert caught.value.kind is FailureKind.WINDOW_CLOSED
    assert caught.value.code == "window_closed"
    assert api.requests == []


async def test_ventana_cerrada_segun_meta_reintenta_con_plantilla(whatsapp):
    sender, api = whatsapp(WINDOW_ERROR, OK, reengagement_template="recontacto")
    result = await sender.send(recipient(BSUID), RenderedPart("Hola"), SendContext(now=NOW))
    assert result == SendResult("wamid.OUT")
    assert [api.json(index)["type"] for index in range(2)] == ["text", "template"]
    assert api.json(1)["recipient"] == BSUID


async def test_ventana_cerrada_segun_meta_sin_plantilla(whatsapp):
    sender, api = whatsapp(WINDOW_ERROR)
    with pytest.raises(DeliveryError) as caught:
        await sender.send(recipient(), RenderedPart("Hola"), SendContext(now=NOW))
    assert (caught.value.kind, caught.value.code) == (FailureKind.WINDOW_CLOSED, "131047")
    assert len(api.requests) == 1


async def test_otros_errores_no_usan_la_plantilla(whatsapp):
    undeliverable = httpx.Response(400, json={"error": {"code": 131026, "message": "Message undeliverable"}})
    sender, api = whatsapp(undeliverable, OK, reengagement_template="recontacto")
    with pytest.raises(DeliveryError) as caught:
        await sender.send(recipient(), RenderedPart("Hola"), SendContext(now=NOW))
    assert caught.value.kind is FailureKind.PERMANENT
    assert len(api.requests) == 1


async def test_respuesta_sin_id_de_mensaje(whatsapp):
    sender, _ = whatsapp(httpx.Response(200, json={"messaging_product": "whatsapp"}))
    assert await sender.send(recipient(), RenderedPart("Hola"), SendContext(now=NOW)) == SendResult(None)


@pytest.mark.parametrize("typing", [True, False])
async def test_notify_processing_marca_leido_y_escribiendo(whatsapp, typing):
    sender, api = whatsapp(httpx.Response(200, json={"success": True}), typing_indicator=typing)
    await sender.notify_processing(recipient(), "wamid.IN1")
    expected = {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.IN1"}
    if typing:
        expected["typing_indicator"] = {"type": "text"}
    assert api.json() == expected
    assert api.last.url.path == f"/v27.0/{PHONE_ID}/messages"


@pytest.mark.parametrize("reply", [httpx.Response(400, json={"error": {"code": 100}}), httpx.ConnectError("caído")])
async def test_notify_processing_nunca_falla(whatsapp, reply):
    sender, api = whatsapp(reply)
    await sender.notify_processing(recipient(), "wamid.IN1")
    assert len(api.requests) == 1


def test_adaptador_completo():
    adapter = build_whatsapp_adapter(CONFIG)
    assert adapter.channel is Channel.WHATSAPP
    assert adapter.accepts_webhooks
    assert adapter.typing is adapter.sender
    assert adapter.capabilities.markup == "whatsapp"
    assert adapter.capabilities.reply_window == timedelta(hours=24)
    assert adapter.capabilities.max_text_chars == 4096
