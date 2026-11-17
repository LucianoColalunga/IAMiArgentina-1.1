"""Telegram Bot API (iamia/integrations/telegram/adapter.py)."""

from __future__ import annotations

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
from iamia.integrations.telegram.adapter import TelegramConfig, TelegramSender, TelegramWebhook, build_telegram_adapter

from .support import NOW, webhook_request

BOT_TOKEN = "123456:test-token"
SECRET = "tg-secret"
CONFIG = TelegramConfig(bot_token=BOT_TOKEN, webhook_secret=SECRET)
CHAT_ID = 555001
TS = int(NOW.timestamp())


def private_message(**fields) -> dict:
    sender = {"id": CHAT_ID, "is_bot": False, "first_name": "Ana", "language_code": "es"}
    return {"message_id": 77, "date": TS, "chat": {"id": CHAT_ID, "type": "private"}, "from": sender, **fields}


def parse(update: dict, received_at: float = 0.0) -> WebhookBatch:
    return TelegramWebhook(CONFIG).parse(webhook_request(update, received_at=received_at))


def test_bot_id_sale_del_token():
    assert CONFIG.bot_id == "123456"
    assert TelegramConfig(bot_token="sin-dos-puntos", webhook_secret=SECRET).bot_id == "bot"


# ------------------------------------------------------------------ autenticación
def test_secret_token_correcto():
    TelegramWebhook(CONFIG).authenticate(webhook_request(b"{}", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}))


@pytest.mark.parametrize(
    "headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "otro"}, {"X-Telegram-Bot-Api-Secret-Token": ""}]
)
def test_secret_token_incorrecto(headers):
    with pytest.raises(WebhookAuthError):
        TelegramWebhook(CONFIG).authenticate(webhook_request(b"{}", headers=headers))


def test_sin_secreto_configurado_rechaza_todo():
    webhook = TelegramWebhook(TelegramConfig(bot_token=BOT_TOKEN, webhook_secret=""))
    with pytest.raises(WebhookAuthError):
        webhook.authenticate(webhook_request(b"{}", headers={"X-Telegram-Bot-Api-Secret-Token": ""}))


def test_no_hay_desafio_de_suscripcion():
    assert TelegramWebhook(CONFIG).challenge(webhook_request(method="GET", query={"hub.challenge": "1"})) is None


# ------------------------------------------------------------------ webhook
def test_mensaje_privado_de_texto():
    batch = parse({"update_id": 900001, "message": private_message(text="Hola", reply_to_message={"message_id": 70})})
    (message,) = batch.messages
    assert message.channel is Channel.TELEGRAM
    assert message.provider_message_id == "900001"  # update_id: clave de idempotencia
    assert message.sender_id == str(CHAT_ID)
    assert message.account_id == "123456"
    assert message.kind is MessageKind.TEXT
    assert message.text == "Hola"
    assert message.sent_at == NOW
    assert message.locale == "es"
    assert message.reply_to == "70"


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": 1, "message": private_message(text="Hola", chat={"id": -100, "type": "group"})},
        {"update_id": 1, "message": private_message(text="Hola", chat={"id": -100, "type": "supergroup"})},
        {"update_id": 1, "message": private_message(text="Hola", **{"from": {"id": 9, "is_bot": True}})},
        {"update_id": 1, "edited_message": private_message(text="Editado")},
        {"update_id": 1, "channel_post": {"message_id": 1, "date": TS, "chat": {"id": -1, "type": "channel"}}},
        {"update_id": 1, "my_chat_member": {}},
    ],
    ids=["grupo", "supergrupo", "otro-bot", "edicion", "canal", "miembro"],
)
def test_actualizaciones_que_se_ignoran(update):
    assert parse(update) == WebhookBatch(ignored=1)


@pytest.mark.parametrize(
    ("fields", "kind", "attachments"),
    [
        (
            {
                "document": {
                    "file_id": "doc-1",
                    "file_name": "partida.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 2048,
                }
            },
            MessageKind.DOCUMENT,
            (Attachment(MessageKind.DOCUMENT, "application/pdf", 2048, "partida.pdf", "doc-1"),),
        ),
        (
            {
                "photo": [
                    {"file_id": "p-chica", "file_size": 100},
                    {"file_id": "p-grande", "file_size": 900},
                    {"file_id": "p-media"},
                ]
            },
            MessageKind.IMAGE,
            (Attachment(MessageKind.IMAGE, size_bytes=900, provider_ref="p-grande"),),
        ),
        (
            {"voice": {"file_id": "v-1", "mime_type": "audio/ogg", "file_size": 300}},
            MessageKind.AUDIO,
            (Attachment(MessageKind.AUDIO, "audio/ogg", 300, provider_ref="v-1"),),
        ),
        (
            {"audio": {"file_id": "a-1", "mime_type": "audio/mpeg"}},
            MessageKind.AUDIO,
            (Attachment(MessageKind.AUDIO, "audio/mpeg", provider_ref="a-1"),),
        ),
        ({"video": {"file_id": "vi-1"}}, MessageKind.VIDEO, (Attachment(MessageKind.VIDEO, provider_ref="vi-1"),)),
        ({"video_note": {"file_id": "vn-1"}}, MessageKind.VIDEO, (Attachment(MessageKind.VIDEO, provider_ref="vn-1"),)),
        ({"location": {"latitude": -34.6, "longitude": -58.4}}, MessageKind.LOCATION, ()),
        ({"venue": {"title": "Registro Civil"}}, MessageKind.LOCATION, ()),
        ({"sticker": {"file_id": "s-1"}}, MessageKind.STICKER, ()),
        ({"contact": {"phone_number": "+5491122334455"}}, MessageKind.CONTACT, ()),
        ({"poll": {"id": "1"}}, MessageKind.UNSUPPORTED, ()),
    ],
    ids=[
        "documento",
        "foto",
        "voz",
        "audio",
        "video",
        "video-nota",
        "ubicacion",
        "lugar",
        "sticker",
        "contacto",
        "encuesta",
    ],
)
def test_adjuntos_solo_metadatos(fields, kind, attachments):
    (message,) = parse({"update_id": 2, "message": private_message(**fields)}).messages
    assert message.kind is kind
    assert message.attachments == attachments
    assert message.text is None


def test_epigrafe_como_texto():
    message = private_message(photo=[{"file_id": "p-1", "file_size": 10}], caption="Mi DNI")
    assert parse({"update_id": 3, "message": message}).messages[0].text == "Mi DNI"


def test_boton_de_teclado_en_chat_privado():
    callback = {
        "id": "cb-1",
        "data": "TURNO" + "x" * 300,
        "message": {"message_id": 1, "date": 0, "chat": {"id": CHAT_ID, "type": "private"}},
    }
    received = (NOW + timedelta(minutes=10)).timestamp()
    (message,) = parse({"update_id": 900002, "callback_query": callback}, received_at=received).messages
    assert message.provider_message_id == "900002"
    assert message.sender_id == str(CHAT_ID)
    assert message.kind is MessageKind.INTERACTIVE
    assert message.text == ("TURNO" + "x" * 300)[:200]
    assert message.sent_at == NOW + timedelta(minutes=10)  # cuenta el momento del toque, no el del mensaje original


@pytest.mark.parametrize(
    "callback",
    [
        {"id": "cb-1", "message": {"chat": {"id": CHAT_ID, "type": "private"}}},
        {"id": "cb-1", "data": "X", "message": {"chat": {"id": -100, "type": "group"}}},
        {"id": "cb-1", "data": "X", "inline_message_id": "abc"},
    ],
    ids=["sin-data", "grupo", "inline"],
)
def test_botones_que_se_ignoran(callback):
    assert parse({"update_id": 4, "callback_query": callback}) == WebhookBatch(ignored=1)


@pytest.mark.parametrize(
    "body",
    [
        b"no es json",
        b"[]",
        b'{"message": {}}',
        b'{"update_id": "1"}',
        b'{"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "from": {}, "text": "sin fecha"}}',
        b'{"update_id": 1, "message": {"date": 1, "chat": {"type": "private"}, "from": {}, "text": "sin chat id"}}',
    ],
    ids=["texto", "lista", "sin-update-id", "update-id-texto", "sin-fecha", "sin-chat-id"],
)
def test_actualizaciones_invalidas(body):
    with pytest.raises(WebhookPayloadError):
        TelegramWebhook(CONFIG).parse(webhook_request(body))


# ------------------------------------------------------------------ envío
RECIPIENT = Recipient(Channel.TELEGRAM, str(CHAT_ID), "123456")


@pytest.fixture
def telegram(fake_api):
    def build(*replies):
        api = fake_api(*replies)
        return TelegramSender(CONFIG, api.http(Channel.TELEGRAM)), api

    return build


async def test_send_message(telegram):
    sender, api = telegram(
        httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHAT_ID}}})
    )
    assert await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW)) == SendResult("42")
    assert api.last.method == "POST"
    assert str(api.last.url) == "https://api.telegram.org/bot123456:test-token/sendMessage"
    assert api.json() == {"chat_id": str(CHAT_ID), "text": "Hola", "link_preview_options": {"is_disabled": True}}


@pytest.mark.parametrize(
    ("status", "body", "kind", "code", "retry_after"),
    [
        (
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 17",
                "parameters": {"retry_after": 17},
            },
            FailureKind.RATE_LIMITED,
            "429",
            17.0,
        ),
        (429, {"ok": False, "error_code": 429}, FailureKind.RATE_LIMITED, "429", 1.0),
        (404, {"ok": False, "error_code": 404, "description": "Not Found"}, FailureKind.AUTH, "404", None),
        (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}, FailureKind.AUTH, "401", None),
        (
            403,
            {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
            FailureKind.PERMANENT,
            "403",
            None,
        ),
        (
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            FailureKind.PERMANENT,
            "400",
            None,
        ),
        (502, {}, FailureKind.RETRYABLE, "502", None),
        (200, {"ok": False, "description": "rara"}, FailureKind.RETRYABLE, "200", None),
    ],
    ids=["429", "429-sin-espera", "404-token", "401", "403-bloqueado", "400", "502", "200-no-ok"],
)
async def test_errores_de_envio(telegram, status, body, kind, code, retry_after):
    sender, _ = telegram(httpx.Response(status, json=body))
    with pytest.raises(DeliveryError) as caught:
        await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert (caught.value.kind, caught.value.code, caught.value.retry_after) == (kind, code, retry_after)


async def test_error_de_red_no_expone_el_token(telegram):
    sender, _ = telegram(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    with pytest.raises(DeliveryError) as caught:
        await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert caught.value.kind is FailureKind.RETRYABLE
    assert "test-token" not in str(caught.value)


async def test_notify_processing_envia_typing(telegram):
    sender, api = telegram(httpx.Response(200, json={"ok": True, "result": True}))
    await sender.notify_processing(RECIPIENT, "900001")
    assert api.last.url.path == "/bot123456:test-token/sendChatAction"
    assert api.json() == {"chat_id": str(CHAT_ID), "action": "typing"}


@pytest.mark.parametrize("reply", [httpx.Response(429, json={"ok": False}), httpx.ReadTimeout("lento")])
async def test_notify_processing_nunca_falla(telegram, reply):
    sender, _ = telegram(reply)
    await sender.notify_processing(RECIPIENT, "900001")


def test_adaptador_completo():
    adapter = build_telegram_adapter(CONFIG)
    assert adapter.channel is Channel.TELEGRAM
    assert adapter.accepts_webhooks
    assert adapter.typing is adapter.sender
    assert adapter.capabilities.max_text_chars == 4096
