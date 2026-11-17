"""WhatsApp Business Platform (Cloud API).

- Entrada: webhook `messages` de la cuenta de WhatsApp Business. Un POST puede traer varios
  mensajes o estados; Meta reintenta hasta 7 días cualquier respuesta distinta de 200, así que
  la API solo valida la firma, encola y contesta. Los duplicados se descartan por `wamid`.
- Identidad: desde 2026 cada mensaje trae un BSUID (`from_user_id`), identificador propio del
  portfolio de negocio, y el teléfono puede no venir si la persona usa nombre de usuario. El
  BSUID es la clave principal; para responder se usa `recipient` (BSUID) o `to` (teléfono).
  Cuando el BSUID cambia, llega un mensaje de sistema y el contacto se re-asocia.
- Ventana de 24 h: fuera de ella solo se pueden enviar plantillas aprobadas. Si hay plantilla de
  re-contacto configurada (categoría utilidad, una variable de cuerpo) se usa; si no, el envío
  falla como WINDOW_CLOSED y la consola lo muestra.
- Mientras se genera la respuesta se marca el mensaje como leído y se muestra "escribiendo".
- Medios: se registran solo los metadatos (tipo, mime, nombre); no se descargan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities, WebhookRequest
from iamia.channels.formatting import PlainTextFormatter
from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    DeliveryState,
    FailureKind,
    IdentityChange,
    InboundMessage,
    MessageKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    StatusUpdate,
    WebhookBatch,
)
from iamia.core.clock import from_unix, utcnow
from iamia.core.errors import WebhookPayloadError
from iamia.core.text import truncate
from iamia.integrations.http import ProviderHttp
from iamia.integrations.meta.common import DEFAULT_GRAPH_VERSION, GraphClient, MetaWebhookVerifier, load_payload

CAPABILITIES = ChannelCapabilities(
    max_text_chars=4096, reply_window=timedelta(hours=24), supports_typing=True, markup="whatsapp"
)
SERVICE_WINDOW = timedelta(hours=24)
SAFETY_MARGIN = timedelta(minutes=5)
TEMPLATE_PARAMETER_MAX_CHARS = 1000

_BSUID = re.compile(r"^[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}$")
_STATUSES = {
    "sent": DeliveryState.SENT,
    "delivered": DeliveryState.DELIVERED,
    "read": DeliveryState.READ,
    "played": DeliveryState.READ,  # notas de voz reproducidas
    "failed": DeliveryState.FAILED,
}
_MEDIA = {
    "image": MessageKind.IMAGE,
    "document": MessageKind.DOCUMENT,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "sticker": MessageKind.STICKER,
}


@dataclass(frozen=True)
class WhatsAppConfig:
    phone_number_id: str
    access_token: str
    app_secret: str
    verify_token: str
    base_url: str = "https://graph.facebook.com"
    api_version: str = DEFAULT_GRAPH_VERSION
    reengagement_template: str = ""
    template_language: str = "es_AR"
    typing_indicator: bool = True
    appsecret_proof: bool = True
    timeout_seconds: float = 15.0


def is_bsuid(value: str) -> bool:
    return bool(_BSUID.match(value))


def addressing(address: str) -> dict[str, str]:
    """`recipient` para BSUID, `to` para teléfono (si vinieran los dos, Meta usa `to`)."""
    return {"recipient": address} if is_bsuid(address) else {"to": address}


def _timestamp(value: Any) -> datetime:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return utcnow()
    return from_unix(seconds) if seconds > 0 else utcnow()


class WhatsAppWebhook(MetaWebhookVerifier):
    def __init__(self, config: WhatsAppConfig) -> None:
        super().__init__(app_secret=config.app_secret, verify_token=config.verify_token)
        self._config = config

    def parse(self, request: WebhookRequest) -> WebhookBatch:
        payload = load_payload(request.body, "whatsapp_business_account")
        if payload is None:
            return WebhookBatch(ignored=1)
        messages: list[InboundMessage] = []
        statuses: list[StatusUpdate] = []
        changes: list[IdentityChange] = []
        ignored = 0
        for entry in payload.get("entry") or []:
            for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
                value = change.get("value") if isinstance(change, dict) else None
                if not isinstance(value, dict) or change.get("field") != "messages":
                    ignored += 1
                    continue
                account = str((value.get("metadata") or {}).get("phone_number_id") or "")
                if self._config.phone_number_id and account != self._config.phone_number_id:
                    ignored += 1  # otro número de la misma cuenta de WhatsApp Business
                    continue
                user_ids = {
                    str(contact.get("wa_id")): str(contact.get("user_id"))
                    for contact in value.get("contacts") or []
                    if isinstance(contact, dict) and contact.get("wa_id") and contact.get("user_id")
                }
                for raw in value.get("messages") or []:
                    parsed = self._message(raw, account, user_ids) if isinstance(raw, dict) else None
                    if isinstance(parsed, InboundMessage):
                        messages.append(parsed)
                    elif isinstance(parsed, IdentityChange):
                        changes.append(parsed)
                    else:
                        ignored += 1
                for raw in value.get("statuses") or []:
                    status = self._status(raw) if isinstance(raw, dict) else None
                    if status is None:
                        ignored += 1
                    else:
                        statuses.append(status)
        return WebhookBatch(
            messages=tuple(messages), statuses=tuple(statuses), ignored=ignored, identity_changes=tuple(changes)
        )

    def _message(
        self, raw: dict[str, Any], account: str, user_ids: dict[str, str]
    ) -> InboundMessage | IdentityChange | None:
        phone = str(raw.get("from") or "")
        sender = str(raw.get("from_user_id") or user_ids.get(phone) or phone)
        message_type = str(raw.get("type") or "")
        if message_type == "system":
            return self._identity_change(raw, sender)
        mid = raw.get("id")
        if not mid or not sender:
            raise WebhookPayloadError("mensaje de WhatsApp sin id o sin remitente")
        if message_type == "reaction":
            return None
        kind, text, attachments, payload = self._content(raw, message_type)
        context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
        return InboundMessage(
            channel=Channel.WHATSAPP,
            provider_message_id=str(mid),
            sender_id=sender,
            account_id=account,
            kind=kind,
            sent_at=_timestamp(raw.get("timestamp")),
            text=text,
            attachments=attachments,
            reply_to=str(context["id"]) if context.get("id") else None,
            extra=MappingProxyType({"payload": payload} if payload else {}),
        )

    @staticmethod
    def _content(raw: dict[str, Any], message_type: str) -> tuple[MessageKind, str | None, tuple[Attachment, ...], str]:
        if message_type == "text":
            return MessageKind.TEXT, str((raw.get("text") or {}).get("body") or ""), (), ""
        if message_type == "interactive":
            interactive = raw.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            return (
                MessageKind.INTERACTIVE,
                str(reply.get("title") or reply.get("id") or ""),
                (),
                str(reply.get("id", "")),
            )
        if message_type == "button":  # botón de respuesta rápida de una plantilla
            button = raw.get("button") or {}
            return MessageKind.INTERACTIVE, str(button.get("text") or ""), (), str(button.get("payload", ""))
        if message_type == "request_welcome":  # la persona abrió el chat por primera vez
            return MessageKind.INTERACTIVE, "/start", (), ""
        if message_type in _MEDIA:
            media = raw.get(message_type) or {}
            kind = _MEDIA[message_type]
            attachment = Attachment(
                kind,
                mime_type=media.get("mime_type"),
                file_name=media.get("filename"),
                provider_ref=media.get("id"),
            )
            caption = media.get("caption")
            return kind, str(caption) if caption else None, (attachment,), ""
        if message_type == "location":
            return MessageKind.LOCATION, None, (), ""
        if message_type == "contacts":
            return MessageKind.CONTACT, None, (), ""
        return MessageKind.UNSUPPORTED, None, (), ""

    @staticmethod
    def _identity_change(raw: dict[str, Any], current: str) -> IdentityChange | None:
        """`user_changed_user_id` / `user_changed_number`: se aceptan las dos ubicaciones de campos
        que describe la documentación (en el mensaje o dentro de `system`)."""
        system = raw.get("system") if isinstance(raw.get("system"), dict) else {}
        if system.get("type") not in ("user_changed_user_id", "user_changed_number"):
            return None
        previous = str(system.get("previous_user_id") or raw.get("previous_user_id") or "")
        new = str(system.get("user_id") or system.get("new_user_id") or current or system.get("new_wa_id") or "")
        if system.get("type") == "user_changed_number" and not previous:
            previous, new = str(raw.get("from") or ""), str(system.get("new_wa_id") or new)
        if not previous or not new or previous == new:
            return None
        return IdentityChange(Channel.WHATSAPP, previous, new, _timestamp(raw.get("timestamp")))

    @staticmethod
    def _status(raw: dict[str, Any]) -> StatusUpdate | None:
        state = _STATUSES.get(str(raw.get("status") or ""))
        if state is None or not raw.get("id"):
            return None
        errors = raw.get("errors") or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        return StatusUpdate(
            channel=Channel.WHATSAPP,
            provider_message_id=str(raw["id"]),
            state=state,
            at=_timestamp(raw.get("timestamp")),
            error_code=str(first["code"]) if first.get("code") is not None else None,
            error_title=str(first.get("title") or "")[:200] or None,
        )


class WhatsAppSender:
    def __init__(self, config: WhatsAppConfig, graph: GraphClient) -> None:
        self._config = config
        self._graph = graph

    @property
    def _path(self) -> str:
        return f"{self._config.phone_number_id}/messages"

    @staticmethod
    def _base(recipient: Recipient) -> dict[str, Any]:
        return {"messaging_product": "whatsapp", "recipient_type": "individual", **addressing(recipient.address)}

    @staticmethod
    def window_open(recipient: Recipient, context: SendContext) -> bool:
        last = recipient.last_inbound_at
        return last is not None and context.now - last < SERVICE_WINDOW - SAFETY_MARGIN

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        base = self._base(recipient)
        if not self.window_open(recipient, context):
            return await self._send_template(base, part)
        message = {**base, "type": "text", "text": {"preview_url": False, "body": part.text}}
        try:
            return self._result(await self._graph.post(self._path, message))
        except DeliveryError as exc:
            if exc.kind is FailureKind.WINDOW_CLOSED and self._config.reengagement_template:
                return await self._send_template(base, part)
            raise

    async def _send_template(self, base: dict[str, Any], part: RenderedPart) -> SendResult:
        name = self._config.reengagement_template
        if not name:
            raise DeliveryError(
                FailureKind.WINDOW_CLOSED,
                "pasaron más de 24 h desde el último mensaje y no hay plantilla de re-contacto",
                code="window_closed",
            )
        # Las variables de plantilla no admiten saltos de línea ni espacios repetidos.
        text = truncate(" ".join(part.text.split()), TEMPLATE_PARAMETER_MAX_CHARS)
        template = {
            "name": name,
            "language": {"code": self._config.template_language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": text}]}],
        }
        return self._result(await self._graph.post(self._path, {**base, "type": "template", "template": template}))

    @staticmethod
    def _result(data: dict[str, Any]) -> SendResult:
        messages = data.get("messages") or []
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        return SendResult(str(first.get("id") or "") or None)

    async def notify_processing(self, recipient: Recipient, provider_message_id: str) -> None:
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read", "message_id": provider_message_id}
        if self._config.typing_indicator:
            payload["typing_indicator"] = {"type": "text"}  # se apaga solo al responder o a los 25 s
        try:
            await self._graph.post(self._path, payload)
        except DeliveryError:
            pass  # indicador visual: nunca bloquea la respuesta

    async def aclose(self) -> None:
        await self._graph.aclose()


def build_whatsapp_adapter(config: WhatsAppConfig, http: ProviderHttp | None = None) -> ChannelAdapter:
    graph = GraphClient(
        Channel.WHATSAPP,
        access_token=config.access_token,
        base_url=config.base_url,
        api_version=config.api_version,
        app_secret=config.app_secret,
        appsecret_proof=config.appsecret_proof,
        timeout=config.timeout_seconds,
        http=http,
    )
    webhook = WhatsAppWebhook(config)
    sender = WhatsAppSender(config, graph)
    return ChannelAdapter(
        channel=Channel.WHATSAPP,
        capabilities=CAPABILITIES,
        formatter=PlainTextFormatter(CAPABILITIES.max_text_chars),
        sender=sender,
        verifier=webhook,
        parser=webhook,
        typing=sender,
    )
