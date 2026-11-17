"""Messenger e Instagram: webhook, ventanas de respuesta y envío.

Cubre iamia/integrations/meta/messaging.py y los adaptadores de instagram/ y messenger/.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from iamia.channels.models import (
    Channel,
    DeliveryError,
    DeliveryState,
    FailureKind,
    MessageKind,
    OutboundContent,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.errors import WebhookPayloadError
from iamia.integrations.instagram.adapter import MAX_TEXT_BYTES, build_instagram_adapter
from iamia.integrations.messenger.adapter import MAX_TEXT_CHARS, build_messenger_adapter
from iamia.integrations.meta import messaging
from iamia.integrations.meta.messaging import MetaMessagingConfig, MetaMessagingWebhook

from .support import NOW, assert_split, webhook_request

PAGE_ID = "123456"
IG_ID = "17841400000000000"
PSID = "24000000000000001"
TS_MS = int(NOW.timestamp() * 1000)
HUMAN_AGENT = {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}


def messenger_config(**overrides) -> MetaMessagingConfig:
    base = MetaMessagingConfig(
        channel=Channel.MESSENGER,
        webhook_object="page",
        account_id=PAGE_ID,
        access_token="test-token",
        app_secret="app-secret",
        verify_token="verify-token",
    )
    return replace(base, **overrides)


def instagram_config(**overrides) -> MetaMessagingConfig:
    return messenger_config(
        **{"channel": Channel.INSTAGRAM, "webhook_object": "instagram", "account_id": IG_ID, **overrides}
    )


def webhook(*messaging_events, entry_id=PAGE_ID, obj="page", **entry_fields) -> dict:
    entry = {"id": entry_id, "time": TS_MS, "messaging": list(messaging_events), **entry_fields}
    return {"object": obj, "entry": [entry]}


def message_event(message: dict, sender: str = PSID) -> dict:
    return {"sender": {"id": sender}, "recipient": {"id": PAGE_ID}, "timestamp": TS_MS, "message": message}


def event(**fields) -> dict:
    return {"sender": {"id": PSID}, "recipient": {"id": PAGE_ID}, "timestamp": TS_MS, **fields}


def parse(payload: dict, config: MetaMessagingConfig | None = None) -> WebhookBatch:
    return MetaMessagingWebhook(config or messenger_config()).parse(webhook_request(payload))


# ------------------------------------------------------------------ webhook
def test_mensaje_de_texto():
    batch = parse(
        webhook(message_event({"mid": "m_1", "text": "Hola, ¿cómo saco el DNI?", "reply_to": {"mid": "m_0"}}))
    )
    assert (batch.statuses, batch.ignored) == ((), 0)
    (message,) = batch.messages
    assert message.channel is Channel.MESSENGER
    assert message.provider_message_id == "m_1"
    assert message.sender_id == PSID
    assert message.account_id == PAGE_ID
    assert message.kind is MessageKind.TEXT
    assert message.text == "Hola, ¿cómo saco el DNI?"
    assert message.sent_at == NOW
    assert message.reply_to == "m_0"
    assert message.extra == {}


def test_respuesta_rapida_es_interactiva_con_payload():
    quick = {"mid": "m_2", "text": "Turnos", "quick_reply": {"payload": "MENU_TURNOS" + "x" * 300}}
    (message,) = parse(webhook(message_event(quick))).messages
    assert message.kind is MessageKind.INTERACTIVE
    assert message.text == "Turnos"
    assert message.extra == {"payload": ("MENU_TURNOS" + "x" * 300)[:200]}


@pytest.mark.parametrize(
    ("types", "kind"),
    [
        (["image"], MessageKind.IMAGE),
        (["video"], MessageKind.VIDEO),
        (["audio"], MessageKind.AUDIO),
        (["file"], MessageKind.DOCUMENT),
        (["location"], MessageKind.LOCATION),
        (["sticker"], MessageKind.STICKER),
        (["ig_reel"], MessageKind.UNSUPPORTED),
        (["share", "image"], MessageKind.UNSUPPORTED),
    ],
)
def test_adjuntos_solo_metadatos(types, kind):
    attachments = [{"type": name, "payload": {"url": "https://lookaside.example.test/adjunto"}} for name in types]
    (message,) = parse(webhook(message_event({"mid": "m_3", "attachments": attachments}))).messages
    assert message.kind is kind
    assert message.text is None
    assert len(message.attachments) == len(types)
    assert all(item.provider_ref is None for item in message.attachments)  # la URL temporal no se guarda


def test_texto_con_adjunto_es_texto():
    raw = {"mid": "m_4", "text": "Mi partida", "attachments": [{"type": "file", "payload": {}}]}
    (message,) = parse(webhook(message_event(raw))).messages
    assert message.kind is MessageKind.TEXT
    assert [item.kind for item in message.attachments] == [MessageKind.DOCUMENT]


def test_mensaje_sin_contenido_reconocible():
    (message,) = parse(webhook(message_event({"mid": "m_5", "is_unsupported": True}))).messages
    assert message.kind is MessageKind.UNSUPPORTED


@pytest.mark.parametrize(
    "raw_event",
    [
        message_event({"mid": "m_e", "text": "eco", "is_echo": True}),
        message_event({"mid": "m_d", "is_deleted": True}),
        message_event({"mid": "m_p", "text": "propio"}, sender=PAGE_ID),
        {"recipient": {"id": PAGE_ID}, "timestamp": TS_MS, "message": {"mid": "m_s", "text": "sin remitente"}},
        event(reaction={"mid": "m_1", "action": "react", "reaction": "love"}),
        event(message_edit={"mid": "m_1", "text": "editado"}),
        event(read={"watermark": TS_MS}),
        event(delivery={"watermark": TS_MS}),
        event(postback={"title": "", "payload": ""}),
        event(postback={"title": "Inicio", "payload": "START"}, sender={"id": PAGE_ID}),
        "no es un objeto",
    ],
    ids=[
        "eco",
        "borrado",
        "propio",
        "sin-remitente",
        "reaccion",
        "edicion",
        "lectura-messenger",
        "entrega-sin-mids",
        "postback-vacio",
        "postback-propio",
        "evento-invalido",
    ],
)
def test_eventos_que_se_ignoran(raw_event):
    assert parse(webhook(raw_event)) == WebhookBatch(ignored=1)


def test_mensaje_sin_mid_es_invalido():
    with pytest.raises(WebhookPayloadError):
        parse(webhook(message_event({"text": "hola"})))


def test_postback():
    raw = event(postback={"mid": "m_pb", "title": "Sacar turno", "payload": "TURNO"})
    (message,) = parse(webhook(raw)).messages
    assert (message.provider_message_id, message.kind, message.text) == ("m_pb", MessageKind.INTERACTIVE, "Sacar turno")
    assert message.extra == {"payload": "TURNO"}


def test_postback_sin_mid_tiene_un_id_estable():
    raw = event(postback={"payload": "GET_STARTED"})
    first = parse(webhook(raw)).messages[0]
    assert first.provider_message_id.startswith("pb-")
    assert first.text == "GET_STARTED"  # sin título se usa el payload
    assert parse(webhook(raw)).messages[0].provider_message_id == first.provider_message_id  # reintento de Meta
    other = parse(webhook(event(postback={"payload": "OTRO"}))).messages[0]
    assert other.provider_message_id != first.provider_message_id


def test_entregas_y_lecturas():
    events = [event(delivery={"mids": ["m_a", "m_b", ""], "watermark": TS_MS}), event(read={"mid": "m_a"})]
    batch = parse(webhook(*events))
    assert batch.statuses == (
        StatusUpdate(Channel.MESSENGER, "m_a", DeliveryState.DELIVERED, NOW),
        StatusUpdate(Channel.MESSENGER, "m_b", DeliveryState.DELIVERED, NOW),
        StatusUpdate(Channel.MESSENGER, "m_a", DeliveryState.READ, NOW),
    )
    assert (batch.messages, batch.ignored) == ((), 0)


def test_standby_cambios_otras_paginas_y_entradas_invalidas_cuentan_como_ignorados():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": PAGE_ID,
                "messaging": [message_event({"mid": "m_1", "text": "hola"})],
                "standby": [message_event({"mid": "m_x", "text": "hilo de otra app"})],
                "changes": [{"field": "feed", "value": {}}],
            },
            {"id": "999999", "messaging": [message_event({"mid": "m_9", "text": "otra página"})]},
            "entrada inválida",
        ],
    }
    batch = parse(payload)
    assert [message.provider_message_id for message in batch.messages] == ["m_1"]
    assert batch.ignored == 4


def test_sin_cuenta_configurada_acepta_cualquier_pagina():
    payload = webhook(message_event({"mid": "m_1", "text": "hola"}), entry_id="777")
    (message,) = parse(payload, messenger_config(account_id="")).messages
    assert message.account_id == "777"


def test_webhook_de_otro_producto_se_ignora():
    assert parse(webhook(message_event({"mid": "m_1", "text": "hola"})), instagram_config()) == WebhookBatch(ignored=1)


def test_instagram_usa_su_objeto_y_su_cuenta():
    payload = webhook(message_event({"mid": "aWdfZAG1", "text": "hola"}), entry_id=IG_ID, obj="instagram")
    (message,) = parse(payload, instagram_config()).messages
    assert (message.channel, message.account_id) == (Channel.INSTAGRAM, IG_ID)


def test_sin_timestamp_usa_la_hora_actual(monkeypatch):
    later = NOW + timedelta(hours=1)
    monkeypatch.setattr(messaging, "utcnow", lambda: later)
    raw = {"sender": {"id": PSID}, "message": {"mid": "m_1", "text": "hola"}}
    assert parse(webhook(raw)).messages[0].sent_at == later


# ------------------------------------------------------------------ reglas de envío
def recipient(elapsed: timedelta | None, channel: Channel = Channel.MESSENGER) -> Recipient:
    return Recipient(channel, PSID, PAGE_ID, last_inbound_at=None if elapsed is None else NOW - elapsed)


@pytest.mark.parametrize(
    ("elapsed", "operator", "human_agent", "expected"),
    [
        (timedelta(hours=1), False, False, {"messaging_type": "RESPONSE"}),
        (timedelta(hours=23, minutes=54), False, False, {"messaging_type": "RESPONSE"}),
        (timedelta(hours=1), True, True, {"messaging_type": "RESPONSE"}),
        (timedelta(hours=23, minutes=56), True, True, HUMAN_AGENT),
        (timedelta(days=6, hours=23), True, True, HUMAN_AGENT),
    ],
)
def test_messaging_type(elapsed, operator, human_agent, expected):
    sender = build_messenger_adapter(messenger_config(human_agent_tag=human_agent)).sender
    assert sender.messaging_type(recipient(elapsed), SendContext(now=NOW, operator_reply=operator)) == expected


@pytest.mark.parametrize(
    ("elapsed", "operator", "human_agent"),
    [
        (timedelta(hours=23, minutes=56), False, False),  # margen de seguridad de 5 minutos
        (timedelta(hours=30), False, True),  # el bot nunca usa HUMAN_AGENT
        (timedelta(hours=30), True, False),  # función Human Agent no habilitada
        (timedelta(days=6, hours=23, minutes=56), True, True),
        (timedelta(days=8), True, True),
        (None, True, True),  # la persona nunca escribió
    ],
)
def test_messaging_type_fuera_de_ventana(elapsed, operator, human_agent):
    sender = build_messenger_adapter(messenger_config(human_agent_tag=human_agent)).sender
    with pytest.raises(DeliveryError) as caught:
        sender.messaging_type(recipient(elapsed), SendContext(now=NOW, operator_reply=operator))
    assert caught.value.kind is FailureKind.WINDOW_CLOSED
    assert caught.value.code == "window_closed"


@pytest.fixture
def messenger(fake_api):
    def build(*replies, **overrides):
        api = fake_api(*replies)
        return build_messenger_adapter(messenger_config(**overrides), api.http(Channel.MESSENGER)), api

    return build


async def test_envio_dentro_de_las_24_horas(messenger):
    adapter, api = messenger(httpx.Response(200, json={"recipient_id": PSID, "message_id": "m_out"}))
    result = await adapter.sender.send(recipient(timedelta(hours=2)), RenderedPart("Hola"), SendContext(now=NOW))
    assert result == SendResult("m_out")
    assert api.last.url.path == f"/v27.0/{PAGE_ID}/messages"
    assert api.last.headers["authorization"] == "Bearer test-token"
    assert "appsecret_proof" in api.last.url.params
    assert api.json() == {"recipient": {"id": PSID}, "message": {"text": "Hola"}, "messaging_type": "RESPONSE"}


async def test_operador_con_human_agent(messenger):
    adapter, api = messenger(httpx.Response(200, json={"message_id": "m_op"}), human_agent_tag=True)
    context = SendContext(now=NOW, operator_reply=True)
    await adapter.sender.send(recipient(timedelta(days=2)), RenderedPart("Te escribe una persona"), context)
    assert api.json() == {
        "recipient": {"id": PSID},
        "message": {"text": "Te escribe una persona"},
        "messaging_type": "MESSAGE_TAG",
        "tag": "HUMAN_AGENT",
    }


async def test_fuera_de_ventana_no_llama_a_la_api(messenger):
    adapter, api = messenger(httpx.Response(200, json={}))
    with pytest.raises(DeliveryError) as caught:
        await adapter.sender.send(recipient(timedelta(hours=25)), RenderedPart("Hola"), SendContext(now=NOW))
    assert caught.value.kind is FailureKind.WINDOW_CLOSED
    assert api.requests == []


async def test_ventana_cerrada_segun_meta(messenger):
    error = {
        "error": {"message": "This message is sent outside of allowed window.", "code": 10, "error_subcode": 2018278}
    }
    adapter, _ = messenger(httpx.Response(400, json=error))
    with pytest.raises(DeliveryError) as caught:
        await adapter.sender.send(recipient(timedelta(hours=1)), RenderedPart("Hola"), SendContext(now=NOW))
    assert caught.value.kind is FailureKind.WINDOW_CLOSED
    assert caught.value.code == "10/2018278"


async def test_respuesta_sin_message_id(messenger):
    adapter, _ = messenger(httpx.Response(200, json={"recipient_id": PSID}))
    result = await adapter.sender.send(recipient(timedelta(hours=1)), RenderedPart("Hola"), SendContext(now=NOW))
    assert result == SendResult(None)


@pytest.mark.parametrize(("typing", "actions"), [(True, ["mark_seen", "typing_on"]), (False, ["mark_seen"])])
async def test_notify_processing(messenger, typing, actions):
    adapter, api = messenger(httpx.Response(200, json={"recipient_id": PSID}), typing_indicator=typing)
    await adapter.typing.notify_processing(recipient(timedelta(hours=1)), "m_1")
    assert [api.json(index) for index in range(len(api.requests))] == [
        {"recipient": {"id": PSID}, "sender_action": action} for action in actions
    ]


@pytest.mark.parametrize("reply", [httpx.Response(500, json={"error": {"code": 2}}), httpx.ConnectError("caído")])
async def test_notify_processing_no_insiste_ni_falla(messenger, reply):
    adapter, api = messenger(reply)
    await adapter.typing.notify_processing(recipient(timedelta(hours=1)), "m_1")
    assert len(api.requests) == 1


# ------------------------------------------------------------------ adaptadores
def test_instagram_valida_la_configuracion():
    with pytest.raises(ValueError, match="Instagram"):
        build_instagram_adapter(messenger_config())
    with pytest.raises(ValueError, match="Instagram"):
        build_instagram_adapter(instagram_config(webhook_object="page"))


def test_messenger_valida_la_configuracion():
    with pytest.raises(ValueError, match="Messenger"):
        build_messenger_adapter(instagram_config())
    with pytest.raises(ValueError, match="Messenger"):
        build_messenger_adapter(messenger_config(webhook_object="instagram"))


@pytest.mark.parametrize("build", [build_instagram_adapter, build_messenger_adapter])
def test_adaptadores_meta_completos(build):
    config = instagram_config() if build is build_instagram_adapter else messenger_config()
    adapter = build(config)
    assert adapter.channel is config.channel
    assert adapter.accepts_webhooks
    assert adapter.verifier is adapter.parser
    assert adapter.typing is adapter.sender
    assert adapter.capabilities.supports_typing
    assert adapter.capabilities.reply_window == timedelta(hours=24)
    assert adapter.capabilities.human_agent_window == timedelta(days=7)


INSTAGRAM_TEXTS = {
    "español": " ".join(["Atención: el trámite del pasaporte se inicia con turno previo en la sede más cercana."] * 40),
    "eñes-sin-espacios": "ñ" * 1500,
    "emojis": "😀" * 600,
    "mezcla": "año 😀 café 漢字 " * 150,
}


@pytest.mark.parametrize("text", INSTAGRAM_TEXTS.values(), ids=list(INSTAGRAM_TEXTS))
def test_instagram_parte_en_1000_bytes_sin_romper_caracteres(text):
    parts = [part.text for part in build_instagram_adapter(instagram_config()).formatter.render(OutboundContent(text))]
    assert len(parts) > 1
    assert all(len(part.encode("utf-8")) <= MAX_TEXT_BYTES for part in parts)
    assert_split(text, parts)


def test_instagram_texto_sin_espacios_se_parte_por_bytes():
    parts = build_instagram_adapter(instagram_config()).formatter.render(OutboundContent("ñ" * 1500))
    assert [part.text for part in parts] == ["ñ" * 500] * 3


def test_messenger_limite_de_1990_caracteres():
    formatter = build_messenger_adapter(messenger_config()).formatter
    exact = "a" * MAX_TEXT_CHARS
    assert [part.text for part in formatter.render(OutboundContent(exact))] == [exact]
    assert [len(part.text) for part in formatter.render(OutboundContent(exact + "b"))] == [MAX_TEXT_CHARS, 1]
    words = " ".join(["Consultá los requisitos del trámite en la web oficial."] * 80)
    parts = [part.text for part in formatter.render(OutboundContent(words))]
    assert len(parts) > 1
    assert all(len(part) <= MAX_TEXT_CHARS for part in parts)
    assert_split(words, parts)
    # El límite de Messenger es en caracteres, no en bytes.
    assert len(formatter.render(OutboundContent("ñ" * MAX_TEXT_CHARS))) == 1
