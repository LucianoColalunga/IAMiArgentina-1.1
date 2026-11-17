"""Telegram Bot API.

- Entrada: webhook configurado con setWebhook y `secret_token`; Telegram manda ese secreto en
  el header X-Telegram-Bot-Api-Secret-Token y se valida en tiempo constante.
- Idempotencia: `update_id` es único por bot, se usa como id del mensaje.
- Solo se atienden chats privados (en grupos el bot no responde: alcance y privacidad).
- Documentos, fotos y audios se registran solo con sus metadatos; no se descargan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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
from iamia.core.clock import from_unix, utcnow
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.http import ProviderHttp, classify_status, safe_json
from iamia.security.signatures import constant_time_equals

logger = logging.getLogger(__name__)

CAPABILITIES = ChannelCapabilities(max_text_chars=4096, supports_typing=True)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    webhook_secret: str
    api_base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 15.0

    @property
    def bot_id(self) -> str:
        return self.bot_token.split(":", 1)[0] if ":" in self.bot_token else "bot"


def _attachment_kind(message: dict[str, Any]) -> tuple[MessageKind, tuple[Attachment, ...]]:
    if "document" in message:
        doc = message["document"] or {}
        return MessageKind.DOCUMENT, (
            Attachment(
                MessageKind.DOCUMENT,
                mime_type=doc.get("mime_type"),
                size_bytes=doc.get("file_size"),
                file_name=doc.get("file_name"),
                provider_ref=doc.get("file_id"),
            ),
        )
    if message.get("photo"):
        biggest = max(message["photo"], key=lambda size: size.get("file_size") or 0)
        return MessageKind.IMAGE, (
            Attachment(MessageKind.IMAGE, size_bytes=biggest.get("file_size"), provider_ref=biggest.get("file_id")),
        )
    for key, kind in (
        ("voice", MessageKind.AUDIO),
        ("audio", MessageKind.AUDIO),
        ("video", MessageKind.VIDEO),
        ("video_note", MessageKind.VIDEO),
    ):
        if key in message:
            media = message[key] or {}
            return kind, (
                Attachment(
                    kind,
                    mime_type=media.get("mime_type"),
                    size_bytes=media.get("file_size"),
                    provider_ref=media.get("file_id"),
                ),
            )
    for key, kind in (
        ("location", MessageKind.LOCATION),
        ("venue", MessageKind.LOCATION),
        ("sticker", MessageKind.STICKER),
        ("contact", MessageKind.CONTACT),
    ):
        if key in message:
            return kind, ()
    if "text" in message:
        return MessageKind.TEXT, ()
    return MessageKind.UNSUPPORTED, ()


class TelegramWebhook:
    def __init__(self, config: TelegramConfig) -> None:
        self._config = config

    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        return None  # Telegram no usa desafío GET: el webhook se registra con setWebhook

    def authenticate(self, request: WebhookRequest) -> None:
        received = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not self._config.webhook_secret or not constant_time_equals(received, self._config.webhook_secret):
            raise WebhookAuthError("secreto de Telegram inválido")

    def parse(self, request: WebhookRequest) -> WebhookBatch:
        try:
            update = json.loads(request.body)
        except ValueError as exc:
            raise WebhookPayloadError("JSON inválido") from exc
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            raise WebhookPayloadError("falta update_id")

        message = update.get("message")
        if isinstance(update.get("callback_query"), dict):
            received = from_unix(request.received_at) if request.received_at else utcnow()
            return self._parse_callback(update["update_id"], update["callback_query"], received)
        if not isinstance(message, dict):
            return WebhookBatch(ignored=1)  # ediciones, canales, encuestas, etc.
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot"):
            return WebhookBatch(ignored=1)
        try:
            kind, attachments = _attachment_kind(message)
            inbound = InboundMessage(
                channel=Channel.TELEGRAM,
                provider_message_id=str(update["update_id"]),
                sender_id=str(chat["id"]),
                account_id=self._config.bot_id,
                kind=kind,
                sent_at=from_unix(message["date"]),
                text=message.get("text") or message.get("caption"),
                attachments=attachments,
                locale=sender.get("language_code"),
                reply_to=str(message["reply_to_message"]["message_id"]) if message.get("reply_to_message") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WebhookPayloadError("mensaje de Telegram incompleto") from exc
        return WebhookBatch(messages=(inbound,))

    def _parse_callback(self, update_id: int, callback: dict[str, Any], received: datetime) -> WebhookBatch:
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or not callback.get("data"):
            return WebhookBatch(ignored=1)
        inbound = InboundMessage(
            channel=Channel.TELEGRAM,
            provider_message_id=str(update_id),
            sender_id=str(chat.get("id")),
            account_id=self._config.bot_id,
            kind=MessageKind.INTERACTIVE,
            sent_at=received,  # la fecha del mensaje original puede ser vieja: cuenta la del toque
            text=str(callback["data"])[:200],
        )
        return WebhookBatch(messages=(inbound,))


class TelegramSender:
    def __init__(self, config: TelegramConfig, http: ProviderHttp | None = None) -> None:
        self._config = config
        self._http = http or ProviderHttp(Channel.TELEGRAM, config.timeout_seconds)

    def _url(self, method: str) -> str:
        return f"{self._config.api_base_url.rstrip('/')}/bot{self._config.bot_token}/{method}"

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        payload = {"chat_id": recipient.address, "text": part.text, "link_preview_options": {"is_disabled": True}}
        response = await self._http.request("POST", self._url("sendMessage"), json=payload)
        data = safe_json(response)
        if response.status_code == 200 and data.get("ok"):
            return SendResult(str((data.get("result") or {}).get("message_id", "")) or None)
        raise self._error(response.status_code, data)

    async def notify_processing(self, recipient: Recipient, provider_message_id: str) -> None:
        try:
            await self._http.request(
                "POST", self._url("sendChatAction"), json={"chat_id": recipient.address, "action": "typing"}
            )
        except DeliveryError:
            pass  # es solo un indicador visual: nunca bloquea la respuesta

    @staticmethod
    def _error(status_code: int, data: dict[str, Any]) -> DeliveryError:
        code = str(data.get("error_code") or status_code)
        description = str(data.get("description") or "error de Telegram")[:200]
        if status_code == 429:
            retry_after = (data.get("parameters") or {}).get("retry_after")
            return DeliveryError(FailureKind.RATE_LIMITED, description, code=code, retry_after=float(retry_after or 1))
        if status_code == 404:  # token inválido: la API responde "Not Found"
            return DeliveryError(FailureKind.AUTH, description, code=code)
        if status_code == 403:  # el usuario bloqueó al bot o lo eliminó
            return DeliveryError(FailureKind.PERMANENT, description, code=code)
        kind = classify_status(status_code) or FailureKind.RETRYABLE
        return DeliveryError(kind, description, code=code)

    async def aclose(self) -> None:
        await self._http.aclose()


def build_telegram_adapter(config: TelegramConfig, http: ProviderHttp | None = None) -> ChannelAdapter:
    webhook = TelegramWebhook(config)
    sender = TelegramSender(config, http)
    return ChannelAdapter(
        channel=Channel.TELEGRAM,
        capabilities=CAPABILITIES,
        formatter=PlainTextFormatter(CAPABILITIES.max_text_chars),
        sender=sender,
        verifier=webhook,
        parser=webhook,
        typing=sender,
    )
