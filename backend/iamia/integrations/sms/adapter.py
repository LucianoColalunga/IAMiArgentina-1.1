"""SMS (proveedor Twilio o compatible).

- Entrada: webhook form-urlencoded firmado con X-Twilio-Signature (HMAC-SHA1 de la URL pública
  exacta más los parámetros ordenados). La URL se configura en SMS_WEBHOOK_URL porque detrás de
  proxies la que ve el servidor no coincide con la que firmó Twilio.
- Estados de entrega: el mismo endpoint recibe los StatusCallback (MessageStatus).
- Salida: texto transliterado a GSM-7 (las tildes agudas pasan el SMS a UCS-2 y lo encarecen)
  y recortado a SMS_MAX_SEGMENTS partes.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl

from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities, HttpReply, WebhookRequest
from iamia.channels.formatting import compose_text, fit_sms, to_gsm7
from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    DeliveryState,
    FailureKind,
    InboundMessage,
    MessageKind,
    OutboundContent,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.clock import utcnow
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.http import ProviderHttp, classify_status, parse_retry_after, safe_json
from iamia.security.signatures import verify_twilio_signature

CAPABILITIES = ChannelCapabilities(max_text_chars=459)
EMPTY_TWIML = HttpReply('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")

_STATUS_MAP = {
    "queued": None,
    "accepted": None,
    "sending": None,
    "sent": DeliveryState.SENT,
    "delivered": DeliveryState.DELIVERED,
    "read": DeliveryState.READ,
    "undelivered": DeliveryState.FAILED,
    "failed": DeliveryState.FAILED,
}
# Errores de Twilio que no se resuelven reintentando (número inválido, destinatario dado de baja...).
_PERMANENT_CODES = {"21211", "21214", "21408", "21610", "21612", "21614", "30003", "30005", "30006"}


@dataclass(frozen=True)
class SmsConfig:
    account_sid: str
    auth_token: str
    webhook_url: str
    from_number: str = ""
    messaging_service_sid: str = ""
    api_base_url: str = "https://api.twilio.com"
    max_segments: int = 3
    timeout_seconds: float = 15.0


class TwilioWebhook:
    def __init__(self, config: SmsConfig) -> None:
        self._config = config

    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        return None

    @staticmethod
    def _params(request: WebhookRequest) -> list[tuple[str, str]]:
        try:
            return parse_qsl(request.body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
        except UnicodeDecodeError as exc:
            raise WebhookPayloadError("cuerpo no UTF-8") from exc

    def authenticate(self, request: WebhookRequest) -> None:
        url = self._config.webhook_url or request.url
        if not verify_twilio_signature(
            self._config.auth_token, url, self._params(request), request.headers.get("x-twilio-signature")
        ):
            raise WebhookAuthError("firma de Twilio inválida")

    def parse(self, request: WebhookRequest) -> WebhookBatch:
        params = dict(self._params(request))
        sid = params.get("MessageSid") or params.get("SmsSid")
        if not sid:
            raise WebhookPayloadError("falta MessageSid")
        status = params.get("MessageStatus") or params.get("SmsStatus")
        if status and "Body" not in params:
            state = _STATUS_MAP.get(status.lower())
            if state is None:
                return WebhookBatch(ignored=1)
            update = StatusUpdate(
                channel=Channel.SMS,
                provider_message_id=sid,
                state=state,
                at=utcnow(),
                error_code=params.get("ErrorCode") or None,
            )
            return WebhookBatch(statuses=(update,))
        sender = params.get("From", "").strip()
        if not sender:
            raise WebhookPayloadError("falta From")
        raw_media = params.get("NumMedia", "0") or "0"
        # Twilio admite hasta 10 adjuntos; un valor inválido no rompe el webhook.
        media = min(int(raw_media), 10) if raw_media.isascii() and raw_media.isdigit() else 0
        attachments = tuple(
            Attachment(MessageKind.IMAGE, mime_type=params.get(f"MediaContentType{index}")) for index in range(media)
        )
        body = params.get("Body", "").strip()
        inbound = InboundMessage(
            channel=Channel.SMS,
            provider_message_id=sid,
            sender_id=sender,
            account_id=params.get("To", ""),
            kind=MessageKind.TEXT if body else (MessageKind.IMAGE if attachments else MessageKind.UNSUPPORTED),
            sent_at=utcnow(),
            text=body or None,
            attachments=attachments,
        )
        return WebhookBatch(messages=(inbound,))


class SmsFormatter:
    def __init__(self, max_segments: int) -> None:
        self._max_segments = max(1, max_segments)

    def render(self, content: OutboundContent) -> list[RenderedPart]:
        text = to_gsm7(compose_text(content))
        return [RenderedPart(fit_sms(text, self._max_segments))]


class TwilioSender:
    def __init__(self, config: SmsConfig, http: ProviderHttp | None = None) -> None:
        self._config = config
        self._http = http or ProviderHttp(Channel.SMS, config.timeout_seconds)

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        cfg = self._config
        data = {"To": recipient.address, "Body": part.text}
        if cfg.messaging_service_sid:
            data["MessagingServiceSid"] = cfg.messaging_service_sid
        else:
            data["From"] = cfg.from_number or recipient.account_id
        url = f"{cfg.api_base_url.rstrip('/')}/2010-04-01/Accounts/{cfg.account_sid}/Messages.json"
        response = await self._http.request("POST", url, data=data, auth=(cfg.account_sid, cfg.auth_token))
        body = safe_json(response)
        if response.status_code in (200, 201):
            return SendResult(str(body.get("sid") or "") or None)
        code = str(body.get("code") or response.status_code)
        detail = str(body.get("message") or "error del proveedor de SMS")[:200]
        if code in _PERMANENT_CODES:
            raise DeliveryError(FailureKind.PERMANENT, detail, code=code)
        kind = classify_status(response.status_code) or FailureKind.RETRYABLE
        raise DeliveryError(kind, detail, code=code, retry_after=parse_retry_after(response.headers.get("retry-after")))

    async def aclose(self) -> None:
        await self._http.aclose()


def build_sms_adapter(config: SmsConfig, http: ProviderHttp | None = None) -> ChannelAdapter:
    webhook = TwilioWebhook(config)
    return ChannelAdapter(
        channel=Channel.SMS,
        capabilities=CAPABILITIES,
        formatter=SmsFormatter(config.max_segments),
        sender=TwilioSender(config, http),
        verifier=webhook,
        parser=webhook,
        webhook_reply=EMPTY_TWIML,
    )
