"""SMS con Twilio (iamia/integrations/sms/adapter.py)."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlencode

import httpx
import pytest

from iamia.channels.formatting import gsm7_length
from iamia.channels.models import (
    Attachment,
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
    SourceLink,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.sms import adapter as sms_adapter
from iamia.integrations.sms.adapter import (
    EMPTY_TWIML,
    SmsConfig,
    SmsFormatter,
    TwilioSender,
    TwilioWebhook,
    build_sms_adapter,
)

from .support import NOW, basic_auth, twilio_signature_header, webhook_request

PUBLIC_URL = "https://iamia.example.test/webhooks/sms"
INTERNAL_URL = "http://10.0.0.5:8000/webhooks/sms"  # lo que ve el servidor detrás del proxy
CONFIG = SmsConfig(account_sid="AC123", auth_token="twilio-token", webhook_url=PUBLIC_URL, from_number="+15005550006")
INBOUND = {
    "MessageSid": "SM123",
    "AccountSid": "AC123",
    "From": "+5491122334455",
    "To": "+15005550006",
    "Body": " Hola, ¿cómo saco turno? ",
    "NumMedia": "0",
    "SmsStatus": "received",
}


def signed(
    params: dict[str, str],
    *,
    url: str = PUBLIC_URL,
    signing_key: str = CONFIG.auth_token,
    request_url: str = INTERNAL_URL,
):
    headers = {"X-Twilio-Signature": twilio_signature_header(signing_key, url, params)}
    return webhook_request(urlencode(params), headers=headers, url=request_url)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(sms_adapter, "utcnow", lambda: NOW)


def parse(params: dict[str, str], config: SmsConfig = CONFIG) -> WebhookBatch:
    return TwilioWebhook(config).parse(webhook_request(urlencode(params)))


# ------------------------------------------------------------------ firma
def test_firma_con_la_url_publica_configurada():
    TwilioWebhook(CONFIG).authenticate(signed(INBOUND))


def test_firma_calculada_sobre_la_url_interna_se_rechaza():
    with pytest.raises(WebhookAuthError):
        TwilioWebhook(CONFIG).authenticate(signed(INBOUND, url=INTERNAL_URL))


def test_sin_url_configurada_usa_la_del_pedido():
    TwilioWebhook(replace(CONFIG, webhook_url="")).authenticate(signed(INBOUND, url=INTERNAL_URL))


@pytest.mark.parametrize(
    "request_",
    [
        replace(signed(INBOUND), body=urlencode({**INBOUND, "Body": "otro texto"}).encode()),
        signed(INBOUND, signing_key="otro-token"),
        webhook_request(urlencode(INBOUND), url=INTERNAL_URL),
    ],
    ids=["parametro-alterado", "otro-token", "sin-firma"],
)
def test_firma_invalida(request_):
    with pytest.raises(WebhookAuthError):
        TwilioWebhook(CONFIG).authenticate(request_)


def test_sin_auth_token_rechaza_todo():
    with pytest.raises(WebhookAuthError):
        TwilioWebhook(replace(CONFIG, auth_token="")).authenticate(signed(INBOUND, signing_key=""))


def test_cuerpo_no_utf8():
    request = webhook_request(b"Body=\xff\xfe", headers={"X-Twilio-Signature": "x"})
    with pytest.raises(WebhookPayloadError):
        TwilioWebhook(CONFIG).authenticate(request)
    with pytest.raises(WebhookPayloadError):
        TwilioWebhook(CONFIG).parse(request)


def test_no_hay_desafio_de_suscripcion():
    assert TwilioWebhook(CONFIG).challenge(webhook_request(method="GET")) is None


# ------------------------------------------------------------------ webhook
def test_sms_entrante():
    (message,) = parse(INBOUND).messages
    assert message.channel is Channel.SMS
    assert message.provider_message_id == "SM123"
    assert message.sender_id == "+5491122334455"
    assert message.account_id == "+15005550006"
    assert message.kind is MessageKind.TEXT
    assert message.text == "Hola, ¿cómo saco turno?"
    assert message.sent_at == NOW
    assert message.attachments == ()


def test_mms_con_imagenes():
    params = {
        **INBOUND,
        "Body": "",
        "NumMedia": "2",
        "MediaContentType0": "image/jpeg",
        "MediaContentType1": "image/png",
    }
    (message,) = parse(params).messages
    assert message.kind is MessageKind.IMAGE
    assert message.text is None
    assert message.attachments == (
        Attachment(MessageKind.IMAGE, mime_type="image/jpeg"),
        Attachment(MessageKind.IMAGE, mime_type="image/png"),
    )


def test_mensaje_vacio_no_soportado():
    (message,) = parse({**INBOUND, "Body": "", "NumMedia": ""}).messages
    assert (message.kind, message.text) == (MessageKind.UNSUPPORTED, None)


def test_sms_sid_como_alternativa():
    params = {key: value for key, value in INBOUND.items() if key != "MessageSid"} | {"SmsSid": "SM999"}
    assert parse(params).messages[0].provider_message_id == "SM999"


@pytest.mark.parametrize(
    ("params", "state", "error_code"),
    [
        ({"MessageStatus": "sent"}, DeliveryState.SENT, None),
        ({"MessageStatus": "delivered"}, DeliveryState.DELIVERED, None),
        ({"MessageStatus": "read"}, DeliveryState.READ, None),
        ({"MessageStatus": "undelivered", "ErrorCode": "30003"}, DeliveryState.FAILED, "30003"),
        ({"MessageStatus": "failed", "ErrorCode": "30008"}, DeliveryState.FAILED, "30008"),
        ({"SmsStatus": "Delivered"}, DeliveryState.DELIVERED, None),
    ],
)
def test_estados_de_entrega(params, state, error_code):
    batch = parse({"MessageSid": "SM1", "AccountSid": "AC123", "To": "+5491122334455", **params})
    assert batch.statuses == (StatusUpdate(Channel.SMS, "SM1", state, NOW, error_code=error_code),)


@pytest.mark.parametrize("status", ["queued", "accepted", "sending", "scheduled"])
def test_estados_intermedios_se_ignoran(status):
    assert parse({"MessageSid": "SM1", "MessageStatus": status}) == WebhookBatch(ignored=1)


@pytest.mark.parametrize(
    "params",
    [{key: value for key, value in INBOUND.items() if key != "MessageSid"}, {**INBOUND, "From": " "}],
    ids=["sin-sid", "sin-remitente"],
)
def test_pedidos_invalidos(params):
    with pytest.raises(WebhookPayloadError):
        parse(params)


# ------------------------------------------------------------------ formato GSM-7
def test_formatter_translitera_a_gsm7():
    (part,) = SmsFormatter(3).render(OutboundContent("¿Cómo está? Año: ñandú — “bien”… 😀"))
    assert part.text == '¿Como esta? Año: ñandu - "bien"... ?'


def test_formatter_agrega_la_fuente():
    content = OutboundContent("Pedí turno online.", (SourceLink("DNI", "https://www.argentina.gob.ar/dni"),))
    (part,) = SmsFormatter(3).render(content)
    assert part.text == "Pedi turno online.\n\nFuente: https://www.argentina.gob.ar/dni"  # "í" no existe en GSM-7


@pytest.mark.parametrize(("segments", "limit"), [(3, 459), (1, 160), (0, 160)])
def test_formatter_recorta_a_la_cantidad_de_partes(segments, limit):
    (part,) = SmsFormatter(segments).render(OutboundContent("Requisitos del trámite " * 60))
    assert gsm7_length(part.text) <= limit
    assert part.text.endswith("...")


# ------------------------------------------------------------------ envío
RECIPIENT = Recipient(Channel.SMS, "+5491122334455", "+15005550006")


@pytest.fixture
def twilio(fake_api):
    def build(*replies, **overrides):
        api = fake_api(*replies)
        return TwilioSender(replace(CONFIG, **overrides), api.http(Channel.SMS)), api

    return build


async def test_envio(twilio):
    sender, api = twilio(httpx.Response(201, json={"sid": "SM456", "status": "queued"}))
    assert await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW)) == SendResult("SM456")
    assert api.last.method == "POST"
    assert str(api.last.url) == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert api.last.headers["authorization"] == basic_auth("AC123", "twilio-token")
    assert api.last.headers["content-type"] == "application/x-www-form-urlencoded"
    assert api.form() == {"To": "+5491122334455", "Body": "Hola", "From": "+15005550006"}


async def test_envio_con_messaging_service(twilio):
    sender, api = twilio(httpx.Response(201, json={"sid": "SM456"}), messaging_service_sid="MG123")
    await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert api.form() == {"To": "+5491122334455", "Body": "Hola", "MessagingServiceSid": "MG123"}


async def test_sin_numero_configurado_usa_el_de_la_cuenta(twilio):
    sender, api = twilio(httpx.Response(201, json={"sid": "SM456"}), from_number="")
    await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert api.form()["From"] == "+15005550006"


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind", "code", "retry_after"),
    [
        (400, {"code": 21211, "message": "Invalid 'To' Phone Number"}, {}, FailureKind.PERMANENT, "21211", None),
        (
            400,
            {"code": 21610, "message": "Attempt to send to unsubscribed recipient"},
            {},
            FailureKind.PERMANENT,
            "21610",
            None,
        ),
        (400, {"code": 21602, "message": "Message body is required."}, {}, FailureKind.PERMANENT, "21602", None),
        (
            429,
            {"code": 20429, "message": "Too Many Requests"},
            {"Retry-After": "5"},
            FailureKind.RATE_LIMITED,
            "20429",
            5.0,
        ),
        (401, {"code": 20003, "message": "Authenticate"}, {}, FailureKind.AUTH, "20003", None),
        (500, {}, {}, FailureKind.RETRYABLE, "500", None),
    ],
    ids=["numero-invalido", "baja", "400-generico", "429", "401", "500"],
)
async def test_errores_de_envio(twilio, status, body, headers, kind, code, retry_after):
    sender, _ = twilio(httpx.Response(status, json=body, headers=headers))
    with pytest.raises(DeliveryError) as caught:
        await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert (caught.value.kind, caught.value.code, caught.value.retry_after) == (kind, code, retry_after)


def test_adaptador_completo():
    adapter = build_sms_adapter(CONFIG)
    assert adapter.channel is Channel.SMS
    assert adapter.accepts_webhooks
    assert adapter.webhook_reply == EMPTY_TWIML
    assert EMPTY_TWIML.media_type == "application/xml"
    assert adapter.capabilities.max_text_chars == 459
