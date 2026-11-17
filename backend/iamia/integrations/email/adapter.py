"""Correo electrónico.

- Entrada: el servidor de correo de la organización (Postfix u otro) entrega cada mensaje a
  POST /webhooks/email con el MIME crudo, firmado con HMAC y timestamp (ver
  scripts/email-a-iamia.py). No hace falta exponer IMAP ni guardar contraseñas de buzones.
- Salida: SMTP autenticado con STARTTLS, respondiendo en el mismo hilo (In-Reply-To).
- Se ignoran respuestas automáticas y rebotes para no generar bucles de correo.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import UTC
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime

from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities, HttpReply, WebhookRequest
from iamia.channels.formatting import compose_text
from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    FailureKind,
    InboundMessage,
    MessageKind,
    OutboundContent,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    WebhookBatch,
)
from iamia.core.clock import utcnow
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.core.text import clean_text
from iamia.security.signatures import verify_timestamped_signature

CAPABILITIES = ChannelCapabilities(max_text_chars=20_000)

_AUTOMATED_SENDERS = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce[s]?)@", re.IGNORECASE)
_QUOTE_MARKERS = re.compile(
    r"^(el .{0,120} escribi[oó]:|on .{0,120} wrote:"
    r"|-{2,}\s*(mensaje original|original message)\s*-{2,}|de:\s.+|from:\s.+)$",
    re.IGNORECASE,
)
_HTML_TAGS = re.compile(r"<[^>]+>")
# Bloques cuyo texto no es parte del mensaje (estilos, scripts, encabezado del documento).
_HTML_HIDDEN = re.compile(r"<(style|script|head)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class EmailConfig:
    inbound_secret: str
    from_address: str
    from_name: str = "Asistente de trámites"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 20.0


def strip_quoted_reply(text: str) -> str:
    """Se queda con lo que escribió la persona, sin el historial citado del correo."""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _QUOTE_MARKERS.match(stripped):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return clean_text("\n".join(kept))


def _is_automated(message: EmailMessage, sender: str) -> bool:
    auto_submitted = str(message.get("Auto-Submitted", "no")).strip().lower()
    precedence = str(message.get("Precedence", "")).strip().lower()
    return (
        auto_submitted != "no"
        or precedence in {"bulk", "junk", "list", "auto_reply"}
        or bool(message.get("List-Id"))
        or bool(_AUTOMATED_SENDERS.match(sender))
    )


class EmailWebhook:
    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        return None

    def authenticate(self, request: WebhookRequest) -> None:
        ok = verify_timestamped_signature(
            self._config.inbound_secret,
            request.body,
            request.headers.get("x-iamia-timestamp"),
            request.headers.get("x-iamia-signature"),
            now=request.received_at or time.time(),
        )
        if not ok:
            raise WebhookAuthError("firma de correo inválida o vencida")

    def parse(self, request: WebhookRequest) -> WebhookBatch:
        try:
            message = message_from_bytes(request.body, policy=policy.default)
        except Exception as exc:
            raise WebhookPayloadError("MIME inválido") from exc
        if not isinstance(message, EmailMessage):
            raise WebhookPayloadError("MIME inválido")
        sender = parseaddr(str(message.get("From", "")))[1].strip().lower()
        if not sender or "@" not in sender:
            raise WebhookPayloadError("el correo no tiene remitente")
        if _is_automated(message, sender):
            return WebhookBatch(ignored=1)

        body_part = message.get_body(preferencelist=("plain", "html"))
        text = ""
        if body_part is not None:
            try:
                content = body_part.get_content()
            except LookupError:  # charset desconocido (p. ej. "unknown-8bit"): se decodifica igual
                content = (body_part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            if body_part.get_content_subtype() == "html":
                content = html.unescape(_HTML_TAGS.sub(" ", _HTML_HIDDEN.sub(" ", content)))
            text = strip_quoted_reply(str(content))
        attachments = tuple(
            Attachment(
                MessageKind.DOCUMENT,
                mime_type=part.get_content_type(),
                size_bytes=len(part.get_payload(decode=True) or b""),
                file_name=part.get_filename(),
            )
            for part in message.iter_attachments()
        )
        message_id = str(message.get("Message-ID", "")).strip() or hashlib.sha256(request.body).hexdigest()
        try:
            sent_at = parsedate_to_datetime(str(message["Date"])) if message.get("Date") else utcnow()
        except (TypeError, ValueError):
            sent_at = utcnow()
        if sent_at.tzinfo is None:  # "-0000" o sin zona (RFC 5322): es UTC; naive rompe la comparación con utcnow()
            sent_at = sent_at.replace(tzinfo=UTC)
        subject = clean_text(str(message.get("Subject", "")))[:200]
        recipient = parseaddr(str(message.get("To", "")))[1].strip().lower() or self._config.from_address
        kind = MessageKind.TEXT if text else (MessageKind.DOCUMENT if attachments else MessageKind.UNSUPPORTED)
        inbound = InboundMessage(
            channel=Channel.EMAIL,
            provider_message_id=message_id[:250],
            sender_id=sender,
            account_id=recipient,
            kind=kind,
            sent_at=sent_at,
            text=text or None,
            attachments=attachments,
            subject=subject or None,
            thread_ref=message_id[:250],
        )
        return WebhookBatch(messages=(inbound,))


class EmailFormatter:
    def render(self, content: OutboundContent) -> list[RenderedPart]:
        text = compose_text(content, include_sources=False)
        if content.sources:
            listed = "\n".join(f"- {source.title}: {source.url}" for source in content.sources)
            text = f"{text}\n\nFuentes oficiales:\n{listed}"
        footer = (
            "\n\n--\nRespuesta automática del asistente de trámites. No incluyas datos personales en tus consultas."
        )
        return [RenderedPart(text + footer)]


class SmtpSender:
    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    def _build(self, recipient: Recipient, part: RenderedPart) -> EmailMessage:
        cfg = self._config
        message = EmailMessage()
        message["From"] = formataddr((cfg.from_name, cfg.from_address))
        message["To"] = recipient.address
        subject = recipient.subject or "Tu consulta"
        message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        message["Auto-Submitted"] = "auto-replied"  # RFC 3834: evita bucles con otros contestadores
        domain = cfg.from_address.partition("@")[2] or None
        message["Message-ID"] = make_msgid(domain=domain)
        if recipient.thread_ref:
            message["In-Reply-To"] = recipient.thread_ref
            message["References"] = recipient.thread_ref
        message.set_content(part.text)
        return message

    def _deliver(self, message: EmailMessage) -> None:
        cfg = self._config
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout_seconds) as smtp:
            smtp.ehlo()
            if cfg.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if cfg.smtp_username:
                smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(message)

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        if not self._config.smtp_host:
            raise DeliveryError(FailureKind.AUTH, "SMTP no configurado", code="smtp_not_configured")
        message = self._build(recipient, part)
        try:
            await asyncio.to_thread(self._deliver, message)
        except smtplib.SMTPAuthenticationError as exc:
            raise DeliveryError(FailureKind.AUTH, "autenticación SMTP rechazada", code=str(exc.smtp_code)) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise DeliveryError(FailureKind.PERMANENT, "destinatario rechazado", code="recipient_refused") from exc
        except smtplib.SMTPResponseException as exc:
            kind = FailureKind.RETRYABLE if 400 <= exc.smtp_code < 500 else FailureKind.PERMANENT
            raise DeliveryError(kind, "el servidor SMTP rechazó el mensaje", code=str(exc.smtp_code)) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(
                FailureKind.RETRYABLE, f"SMTP no disponible: {type(exc).__name__}", code="smtp"
            ) from exc
        return SendResult(str(message["Message-ID"]))


def build_email_adapter(config: EmailConfig) -> ChannelAdapter:
    webhook = EmailWebhook(config)
    return ChannelAdapter(
        channel=Channel.EMAIL,
        capabilities=CAPABILITIES,
        formatter=EmailFormatter(),
        sender=SmtpSender(config),
        verifier=webhook,
        parser=webhook,
    )
