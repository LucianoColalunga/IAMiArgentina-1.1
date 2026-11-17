"""Messenger e Instagram comparten el formato del webhook (entry[].messaging[]) y la Send API.

Diferencias que resuelve la configuración: el `object` del webhook ("page" o "instagram"), la
cuenta propia (id de página o de la cuenta profesional de Instagram) y el largo máximo.

Reglas de envío de ambas plataformas:
- Respuesta libre (RESPONSE) dentro de las 24 h posteriores al último mensaje del usuario.
- Fuera de esa ventana solo puede escribir una persona, con la etiqueta HUMAN_AGENT y hasta
  7 días (requiere la función "Human Agent" aprobada por Meta). El bot nunca usa la etiqueta.
- Los ecos (mensajes propios) y los mensajes borrados se descartan.
- Las URL de adjuntos son enlaces temporales a contenido del usuario: no se guardan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from iamia.channels.contracts import WebhookRequest
from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    DeliveryState,
    FailureKind,
    InboundMessage,
    MessageKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.clock import from_unix_ms, utcnow
from iamia.core.errors import WebhookPayloadError
from iamia.core.text import stable_hash
from iamia.integrations.meta.common import DEFAULT_GRAPH_VERSION, GraphClient, MetaWebhookVerifier, load_payload

STANDARD_WINDOW = timedelta(hours=24)
HUMAN_AGENT_WINDOW = timedelta(days=7)
SAFETY_MARGIN = timedelta(minutes=5)  # no apurar el borde de la ventana: el reloj de Meta manda

_ATTACHMENT_KINDS = {
    "image": MessageKind.IMAGE,
    "video": MessageKind.VIDEO,
    "audio": MessageKind.AUDIO,
    "file": MessageKind.DOCUMENT,
    "location": MessageKind.LOCATION,
    "sticker": MessageKind.STICKER,
}


@dataclass(frozen=True)
class MetaMessagingConfig:
    channel: Channel
    webhook_object: str  # "page" (Messenger) o "instagram"
    account_id: str  # id de la página o de la cuenta profesional de Instagram
    access_token: str
    app_secret: str
    verify_token: str
    base_url: str = "https://graph.facebook.com"
    api_version: str = DEFAULT_GRAPH_VERSION
    human_agent_tag: bool = False
    typing_indicator: bool = True
    appsecret_proof: bool = True
    timeout_seconds: float = 15.0


def _sent_at(event: dict[str, Any]) -> datetime:
    timestamp = event.get("timestamp")
    return from_unix_ms(timestamp) if isinstance(timestamp, int | float) and timestamp > 0 else utcnow()


class MetaMessagingWebhook(MetaWebhookVerifier):
    def __init__(self, config: MetaMessagingConfig) -> None:
        super().__init__(app_secret=config.app_secret, verify_token=config.verify_token)
        self._config = config

    def parse(self, request: WebhookRequest) -> WebhookBatch:
        payload = load_payload(request.body, self._config.webhook_object)
        if payload is None:
            return WebhookBatch(ignored=1)
        messages: list[InboundMessage] = []
        statuses: list[StatusUpdate] = []
        ignored = 0
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                ignored += 1
                continue
            account = str(entry.get("id") or "")
            if self._config.account_id and account != self._config.account_id:
                ignored += 1  # otra página o cuenta suscripta a la misma app
                continue
            # "standby": eventos de hilos que controla otra app; "changes": comentarios, menciones.
            ignored += len(entry.get("standby") or []) + len(entry.get("changes") or [])
            for event in entry.get("messaging") or []:
                parsed = self._event(event, account) if isinstance(event, dict) else None
                if isinstance(parsed, InboundMessage):
                    messages.append(parsed)
                elif parsed:
                    statuses.extend(parsed)
                else:
                    ignored += 1
        return WebhookBatch(messages=tuple(messages), statuses=tuple(statuses), ignored=ignored)

    def _event(self, event: dict[str, Any], account: str) -> InboundMessage | list[StatusUpdate] | None:
        sender = str((event.get("sender") or {}).get("id") or "")
        sent_at = _sent_at(event)
        if isinstance(event.get("message"), dict):
            return self._message(event["message"], sender, account, sent_at)
        if isinstance(event.get("postback"), dict):
            return self._postback(event["postback"], sender, account, sent_at)
        channel = self._config.channel
        if isinstance(event.get("delivery"), dict):
            mids = event["delivery"].get("mids") or []
            return [StatusUpdate(channel, str(mid), DeliveryState.DELIVERED, sent_at) for mid in mids if mid] or None
        if isinstance(event.get("read"), dict) and event["read"].get("mid"):
            return [StatusUpdate(channel, str(event["read"]["mid"]), DeliveryState.READ, sent_at)]
        return None  # reacciones, referencias de anuncios, cambios de control del hilo

    def _message(self, message: dict[str, Any], sender: str, account: str, sent_at: datetime) -> InboundMessage | None:
        if message.get("is_echo") or message.get("is_deleted") or not sender or sender == account:
            return None
        mid = message.get("mid")
        if not mid:
            raise WebhookPayloadError("mensaje sin mid")
        text = message.get("text") if isinstance(message.get("text"), str) else None
        quick_reply = message.get("quick_reply") if isinstance(message.get("quick_reply"), dict) else {}
        attachments = tuple(
            Attachment(_ATTACHMENT_KINDS.get(str(item.get("type")), MessageKind.UNSUPPORTED))
            for item in message.get("attachments") or []
            if isinstance(item, dict)
        )
        if quick_reply:
            kind = MessageKind.INTERACTIVE
        elif text:
            kind = MessageKind.TEXT
        elif attachments:
            kind = attachments[0].kind
        else:
            kind = MessageKind.UNSUPPORTED
        reply_to = (message.get("reply_to") or {}).get("mid") if isinstance(message.get("reply_to"), dict) else None
        extra = {"payload": str(quick_reply.get("payload", ""))[:200]} if quick_reply else {}
        return InboundMessage(
            channel=self._config.channel,
            provider_message_id=str(mid),
            sender_id=sender,
            account_id=account,
            kind=kind,
            sent_at=sent_at,
            text=text,
            attachments=attachments,
            reply_to=str(reply_to) if reply_to else None,
            extra=MappingProxyType(extra),
        )

    def _postback(
        self, postback: dict[str, Any], sender: str, account: str, sent_at: datetime
    ) -> InboundMessage | None:
        text = postback.get("title") or postback.get("payload")
        if not sender or sender == account or not text:
            return None
        payload = str(postback.get("payload", ""))[:200]
        mid = postback.get("mid") or "pb-" + stable_hash(sender, sent_at.isoformat(), payload)[:40]
        return InboundMessage(
            channel=self._config.channel,
            provider_message_id=str(mid),
            sender_id=sender,
            account_id=account,
            kind=MessageKind.INTERACTIVE,
            sent_at=sent_at,
            text=str(text)[:200],
            extra=MappingProxyType({"payload": payload}),
        )


class MetaMessagingSender:
    def __init__(self, config: MetaMessagingConfig, graph: GraphClient) -> None:
        self._config = config
        self._graph = graph

    @property
    def _path(self) -> str:
        return f"{self._config.account_id}/messages"

    def messaging_type(self, recipient: Recipient, context: SendContext) -> dict[str, str]:
        elapsed = None if recipient.last_inbound_at is None else context.now - recipient.last_inbound_at
        if elapsed is not None and elapsed < STANDARD_WINDOW - SAFETY_MARGIN:
            return {"messaging_type": "RESPONSE"}
        if (
            context.operator_reply
            and self._config.human_agent_tag
            and elapsed is not None
            and elapsed < HUMAN_AGENT_WINDOW - SAFETY_MARGIN
        ):
            return {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}
        raise DeliveryError(
            FailureKind.WINDOW_CLOSED, "fuera de la ventana de respuesta del canal", code="window_closed"
        )

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        body: dict[str, Any] = {
            "recipient": {"id": recipient.address},
            "message": {"text": part.text},
            **self.messaging_type(recipient, context),
        }
        data = await self._graph.post(self._path, body)
        return SendResult(str(data.get("message_id") or "") or None)

    async def notify_processing(self, recipient: Recipient, provider_message_id: str) -> None:
        actions = ["mark_seen"] + (["typing_on"] if self._config.typing_indicator else [])
        for action in actions:
            try:
                await self._graph.post(self._path, {"recipient": {"id": recipient.address}, "sender_action": action})
            except DeliveryError:
                return  # indicador visual: si falla no se insiste ni se bloquea la respuesta

    async def aclose(self) -> None:
        await self._graph.aclose()


def build_graph_client(config: MetaMessagingConfig, http: Any = None) -> GraphClient:
    return GraphClient(
        config.channel,
        access_token=config.access_token,
        base_url=config.base_url,
        api_version=config.api_version,
        app_secret=config.app_secret,
        appsecret_proof=config.appsecret_proof,
        timeout=config.timeout_seconds,
        http=http,
    )
