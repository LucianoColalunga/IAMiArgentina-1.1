"""X (mensajes directos).

- Entrada: X Activity API (XAA), suscripción `dm.received` creada con POST
  /2/activity/subscriptions. Durante la migración también se acepta el formato de la Account
  Activity API, que deja de funcionar el 28/2/2027.
- CRC: GET con `crc_token`; se responde {"response_token": "sha256=" + base64(HMAC-SHA256)}.
- Firma: X-Twitter-Webhooks-Signature-OAuth2 (client secret de OAuth 2.0) o la heredada
  X-Twitter-Webhooks-Signature (consumer secret). X_WEBHOOK_SECRET es el secreto que corresponda
  a cómo se registró el webhook; se acepta el pedido si alguna de las firmas coincide.
- Salida: POST /2/dm_conversations/with/{id}/messages con token de usuario OAuth 2.0 (dm.write).
- Límites de X: 15 DMs cada 15 minutos y 1440 por día para toda la cuenta. Es el canal de menor
  capacidad: el throttle del dispatcher respeta esos valores y el excedente espera en la cola.
- Política de X: el bot solo responde a un DM previo, ofrece la baja (STOP) y las respuestas
  generadas con IA requieren aprobación previa de X.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities, HttpReply, WebhookRequest
from iamia.channels.formatting import PlainTextFormatter
from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    FailureKind,
    InboundMessage,
    MessageKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    WebhookBatch,
)
from iamia.core.clock import from_unix_ms, utcnow
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.http import ProviderHttp, classify_status, parse_retry_after, safe_json
from iamia.integrations.x.tokens import AccessTokenProvider
from iamia.security.signatures import verify_x_signature, x_crc_response_token

CAPABILITIES = ChannelCapabilities(max_text_chars=10_000)
_MAX_CRC_LENGTH = 256
_ACCOUNT_PROBLEMS = ("client-forbidden", "unsupported-authentication", "not-authorized-for-resource")


@dataclass(frozen=True)
class XConfig:
    account_id: str  # id numérico de la cuenta de X que atiende
    webhook_secret: str
    api_base_url: str = "https://api.x.com"
    timeout_seconds: float = 15.0


def _iso(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class XWebhook:
    def __init__(self, config: XConfig) -> None:
        self._config = config

    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        crc = request.query.get("crc_token", "")
        if not crc or len(crc) > _MAX_CRC_LENGTH or not self._config.webhook_secret:
            return None
        token = x_crc_response_token(self._config.webhook_secret, crc)
        return HttpReply(json.dumps({"response_token": token}), media_type="application/json")

    def authenticate(self, request: WebhookRequest) -> None:
        headers = (
            request.headers.get("x-twitter-webhooks-signature-oauth2"),
            request.headers.get("x-twitter-webhooks-signature"),
        )
        if not any(verify_x_signature(self._config.webhook_secret, request.body, header) for header in headers):
            raise WebhookAuthError("firma de X inválida")

    def parse(self, request: WebhookRequest) -> WebhookBatch:
        try:
            payload = json.loads(request.body)
        except ValueError as exc:
            raise WebhookPayloadError("JSON inválido") from exc
        if not isinstance(payload, dict):
            raise WebhookPayloadError("estructura de webhook desconocida")
        events = self._events(payload)
        if events is None:
            return WebhookBatch(ignored=1)
        messages: list[InboundMessage] = []
        ignored = 0
        for event in events:
            parsed = self._direct_message(event) if isinstance(event, dict) else None
            if parsed is None:
                ignored += 1
            else:
                messages.append(parsed)
        return WebhookBatch(messages=tuple(messages), ignored=ignored)

    def _events(self, payload: dict[str, Any]) -> list[Any] | None:
        data = payload.get("data")
        if isinstance(data, dict):  # XAA
            owner = str((data.get("filter") or {}).get("user_id") or "")
            if data.get("event_type") != "dm.received" or (
                self._config.account_id and owner != self._config.account_id
            ):
                return None
            inner = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            return list(inner.get("direct_message_events") or [])
        if "direct_message_events" in payload:  # Account Activity API (formato heredado)
            if self._config.account_id and str(payload.get("for_user_id") or "") != self._config.account_id:
                return None
            return list(payload.get("direct_message_events") or [])
        return None

    def _direct_message(self, event: dict[str, Any]) -> InboundMessage | None:
        if event.get("type") == "message_create":  # formato v1.1 dentro del sobre
            create = event.get("message_create") or {}
            sender = str(create.get("sender_id") or "")
            data = create.get("message_data") or {}
            text = data.get("text")
            sent_at = (
                from_unix_ms(int(event["created_timestamp"]))
                if str(event.get("created_timestamp", "")).isdigit()
                else utcnow()
            )
            quick = data.get("quick_reply_response") or {}
            media = data.get("attachment")
            thread = None
        elif event.get("event_type") == "MessageCreate":  # formato v2
            sender = str(event.get("sender_id") or "")
            text = event.get("text")
            sent_at = _iso(event.get("created_at"))
            quick = {}
            media = (event.get("attachments") or {}).get("media_keys")
            thread = event.get("dm_conversation_id")
        else:
            return None
        if not sender or sender == self._config.account_id or not event.get("id"):
            return None  # mensajes propios o incompletos
        attachments = (Attachment(MessageKind.IMAGE),) if media else ()
        if quick:
            kind = MessageKind.INTERACTIVE
        elif text:
            kind = MessageKind.TEXT
        else:
            kind = MessageKind.IMAGE if attachments else MessageKind.UNSUPPORTED
        payload = str(quick.get("metadata", ""))[:200] if quick else ""
        return InboundMessage(
            channel=Channel.X,
            provider_message_id=str(event["id"]),
            sender_id=sender,
            account_id=self._config.account_id,
            kind=kind,
            sent_at=sent_at,
            text=str(text) if text else None,
            attachments=attachments,
            thread_ref=str(thread) if thread else None,
            extra=MappingProxyType({"payload": payload} if payload else {}),
        )


class XSender:
    def __init__(self, config: XConfig, tokens: AccessTokenProvider, http: ProviderHttp | None = None) -> None:
        self._config = config
        self._tokens = tokens
        self._http = http or ProviderHttp(Channel.X, config.timeout_seconds)

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        token = await self._tokens.access_token()
        url = (
            f"{self._config.api_base_url.rstrip('/')}/2/dm_conversations/with/"
            f"{quote(recipient.address, safe='')}/messages"
        )
        response = await self._http.request(
            "POST", url, json={"text": part.text}, headers={"Authorization": f"Bearer {token}"}
        )
        data = safe_json(response)
        if response.status_code in (200, 201):
            return SendResult(str((data.get("data") or {}).get("dm_event_id") or "") or None)
        if response.status_code == 401:
            retry = await self._tokens.invalidate(token)
            kind = FailureKind.RETRYABLE if retry else FailureKind.AUTH
            raise DeliveryError(kind, "X rechazó el token de usuario", code="401")
        raise self._error(response.status_code, data, response.headers)

    @staticmethod
    def _error(status_code: int, data: dict[str, Any], headers: Any) -> DeliveryError:
        problem = str(data.get("type") or "")
        detail = str(data.get("detail") or data.get("title") or "error de X")[:200]
        code = str(status_code) + (f":{problem.rsplit('/', 1)[-1]}" if problem else "")
        if status_code == 429:
            reset = headers.get("x-rate-limit-reset")
            wait = float(reset) - time.time() if reset and reset.isascii() and reset.isdigit() else None
            retry_after = parse_retry_after(headers.get("retry-after")) or (max(1.0, wait) if wait else 60.0)
            return DeliveryError(FailureKind.RATE_LIMITED, detail, code=code, retry_after=retry_after)
        if status_code == 403:
            account_problem = any(name in problem for name in _ACCOUNT_PROBLEMS)
            # 403 sin problema de cuenta: el usuario no acepta DMs de la cuenta o la bloqueó.
            return DeliveryError(FailureKind.AUTH if account_problem else FailureKind.PERMANENT, detail, code=code)
        return DeliveryError(classify_status(status_code) or FailureKind.RETRYABLE, detail, code=code)

    async def aclose(self) -> None:
        await self._http.aclose()
        closer = getattr(self._tokens, "aclose", None)
        if closer is not None:
            await closer()


def build_x_adapter(config: XConfig, tokens: AccessTokenProvider, http: ProviderHttp | None = None) -> ChannelAdapter:
    webhook = XWebhook(config)
    return ChannelAdapter(
        channel=Channel.X,
        capabilities=CAPABILITIES,
        formatter=PlainTextFormatter(CAPABILITIES.max_text_chars),
        sender=XSender(config, tokens, http),
        verifier=webhook,
        parser=webhook,
    )
