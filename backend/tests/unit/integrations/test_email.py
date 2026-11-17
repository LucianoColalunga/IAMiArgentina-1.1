"""Correo electrónico (iamia/integrations/email/adapter.py)."""

from __future__ import annotations

import hashlib
import hmac
import smtplib
import time
from dataclasses import replace
from datetime import UTC, timedelta
from email.message import EmailMessage

import pytest

from iamia.channels.models import (
    Attachment,
    Channel,
    DeliveryError,
    FailureKind,
    MessageKind,
    OutboundContent,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
    SourceLink,
    WebhookBatch,
)
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.email import adapter as email_adapter
from iamia.integrations.email.adapter import (
    EmailConfig,
    EmailFormatter,
    EmailWebhook,
    SmtpSender,
    build_email_adapter,
    strip_quoted_reply,
)

from .support import NOW, webhook_request

SECRET = "mail-secret"
CONFIG = EmailConfig(
    inbound_secret=SECRET,
    from_address="tramites@example.gob.ar",
    smtp_host="smtp.example.test",
    smtp_username="asistente",
    smtp_password="smtp-pass",
)
TS = int(NOW.timestamp())


def raw_email(body: str = "Hola, quiero sacar el DNI.", *, subtype: str = "plain", **headers: str | None) -> bytes:
    message = EmailMessage()
    defaults = {
        "From": "Ana Pérez <Ana.Perez@Example.com>",
        "To": "tramites@example.gob.ar",
        "Subject": "Consulta DNI",
        "Date": "Mon, 16 Nov 2026 12:00:00 +0000",
        "Message-ID": "<abc123@example.com>",
    }
    for name, value in {**defaults, **{key.replace("_", "-"): val for key, val in headers.items()}}.items():
        if value is not None:
            message[name] = value
    message.set_content(body, subtype=subtype)
    return message.as_bytes()


def parse(body: bytes) -> WebhookBatch:
    return EmailWebhook(CONFIG).parse(webhook_request(body))


# ------------------------------------------------------------------ autenticación
def signed_request(body: bytes, *, secret: str = SECRET, timestamp: int = TS, received_at: float = TS):
    signature = "v1=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {"X-IAMIA-Timestamp": str(timestamp), "X-IAMIA-Signature": signature}
    return webhook_request(body, headers=headers, received_at=received_at)


def test_firma_valida():
    EmailWebhook(CONFIG).authenticate(signed_request(raw_email()))


def test_sin_received_at_usa_la_hora_actual():
    now = int(time.time())
    EmailWebhook(CONFIG).authenticate(signed_request(raw_email(), timestamp=now, received_at=0))


@pytest.mark.parametrize(
    "request_",
    [
        signed_request(raw_email(), received_at=TS + 301),
        signed_request(raw_email(), received_at=TS - 301),
        signed_request(raw_email(), secret="otro-secreto"),
        replace(signed_request(raw_email()), body=raw_email("Texto alterado")),
        webhook_request(raw_email(), received_at=TS),
    ],
    ids=["vencida", "del-futuro", "otro-secreto", "cuerpo-alterado", "sin-firma"],
)
def test_firma_invalida(request_):
    with pytest.raises(WebhookAuthError):
        EmailWebhook(CONFIG).authenticate(request_)


def test_sin_secreto_configurado_rechaza_todo():
    with pytest.raises(WebhookAuthError):
        EmailWebhook(replace(CONFIG, inbound_secret="")).authenticate(signed_request(raw_email(), secret=""))


def test_no_hay_desafio_de_suscripcion():
    assert EmailWebhook(CONFIG).challenge(webhook_request(method="GET")) is None


# ------------------------------------------------------------------ citas del historial
@pytest.mark.parametrize(
    "marker",
    [
        "El lun, 16 nov 2026 a las 10:00, Trámites <tramites@example.gob.ar> escribió:",
        "On Mon, Nov 16, 2026 at 10:00 AM Trámites <tramites@example.gob.ar> wrote:",
        "-----Mensaje original-----",
        "----- Original Message -----",
        "De: Trámites <tramites@example.gob.ar>",
        "From: Trámites <tramites@example.gob.ar>",
    ],
)
def test_strip_quoted_reply_corta_en_el_historial(marker):
    text = f"Gracias, ¿y el pasaporte?\n\n{marker}\nRespuesta anterior del asistente."
    assert strip_quoted_reply(text) == "Gracias, ¿y el pasaporte?"


def test_strip_quoted_reply_descarta_lineas_citadas():
    assert strip_quoted_reply("Respuesta\n> línea citada\n>> más cita\nOtra línea") == "Respuesta\nOtra línea"


# ------------------------------------------------------------------ webhook
def test_correo_de_texto():
    batch = parse(raw_email("Hola, quiero sacar el DNI.\n\nEl lun, 16 nov 2026, Trámites escribió:\n> anterior"))
    (message,) = batch.messages
    assert message.channel is Channel.EMAIL
    assert message.provider_message_id == "<abc123@example.com>"
    assert message.thread_ref == "<abc123@example.com>"
    assert message.sender_id == "ana.perez@example.com"
    assert message.account_id == "tramites@example.gob.ar"
    assert message.kind is MessageKind.TEXT
    assert message.text == "Hola, quiero sacar el DNI."
    assert message.subject == "Consulta DNI"
    assert message.sent_at == NOW
    assert message.attachments == ()


def test_correo_solo_html():
    body = "<html><body><p>Hola <b>equipo</b>,</p><div>¿Qué necesito para el pasaporte?</div></body></html>"
    (message,) = parse(raw_email(body, subtype="html")).messages
    assert message.text == "Hola equipo , ¿Qué necesito para el pasaporte?"


def test_prefiere_texto_plano_a_html():
    message = EmailMessage()
    message["From"] = "ana@example.com"
    message.set_content("Versión en texto")
    message.add_alternative("<p>Versión <b>HTML</b></p>", subtype="html")
    assert parse(message.as_bytes()).messages[0].text == "Versión en texto"


def test_adjuntos_solo_metadatos():
    message = EmailMessage()
    message["From"] = "ana@example.com"
    message.set_content("Adjunto la partida.")
    message.add_attachment(b"%PDF-1.4 falso", maintype="application", subtype="pdf", filename="partida.pdf")
    (inbound,) = parse(message.as_bytes()).messages
    assert inbound.kind is MessageKind.TEXT
    assert inbound.attachments == (Attachment(MessageKind.DOCUMENT, "application/pdf", 14, "partida.pdf"),)


def test_solo_adjunto_es_documento():
    message = EmailMessage()
    message["From"] = "ana@example.com"
    message.set_content("")
    message.add_attachment(b"foto", maintype="image", subtype="jpeg", filename="dni.jpg")
    (inbound,) = parse(message.as_bytes()).messages
    assert (inbound.kind, inbound.text) == (MessageKind.DOCUMENT, None)


def test_correo_vacio_no_soportado():
    (message,) = parse(raw_email("\n")).messages
    assert (message.kind, message.text) == (MessageKind.UNSUPPORTED, None)


def test_valores_por_defecto(monkeypatch):
    monkeypatch.setattr(email_adapter, "utcnow", lambda: NOW + timedelta(hours=1))
    body = raw_email(Message_ID=None, To=None, Date=None, Subject=None)
    (message,) = parse(body).messages
    assert message.provider_message_id == hashlib.sha256(body).hexdigest()
    assert message.account_id == "tramites@example.gob.ar"  # sin To: la casilla propia
    assert message.sent_at == NOW + timedelta(hours=1)
    assert message.subject is None


def test_fecha_invalida_usa_la_hora_actual(monkeypatch):
    monkeypatch.setattr(email_adapter, "utcnow", lambda: NOW + timedelta(hours=1))
    assert parse(raw_email(Date="ayer a la tarde")).messages[0].sent_at == NOW + timedelta(hours=1)


@pytest.mark.parametrize("date", ["Mon, 16 Nov 2026 12:00:00 -0000", "Mon, 16 Nov 2026 12:00:00"])
def test_fecha_sin_zona_horaria_se_toma_como_utc(date):
    # RFC 5322: "-0000" indica UTC sin zona de origen. Una fecha naive rompía la comparación con utcnow().
    sent_at = parse(raw_email(Date=date)).messages[0].sent_at
    assert sent_at.tzinfo is not None
    assert sent_at == NOW


def test_charset_desconocido_no_rompe_el_webhook():
    # RFC 1428: algunos servidores marcan el texto de 8 bits sin charset como "unknown-8bit".
    raw = (
        "From: ana@example.com\r\n"
        "To: tramites@example.gob.ar\r\n"
        "Subject: Consulta\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=unknown-8bit\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        "¿Cómo saco el DNI?\r\n"
    ).encode()
    (message,) = parse(raw).messages
    assert message.text == "¿Cómo saco el DNI?"


def test_asunto_limpio_y_recortado():
    assert parse(raw_email(Subject="Consulta \t sobre   DNI")).messages[0].subject == "Consulta sobre DNI"
    assert parse(raw_email(Subject="x" * 300)).messages[0].subject == "x" * 200


@pytest.mark.parametrize("sender", [None, "sin arroba", ""])
def test_sin_remitente_valido(sender):
    with pytest.raises(WebhookPayloadError):
        parse(raw_email(From=sender))


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto_Submitted": "auto-replied"},
        {"Auto_Submitted": "auto-generated"},
        {"Precedence": "bulk"},
        {"Precedence": "junk"},
        {"Precedence": "list"},
        {"Precedence": "auto_reply"},
        {"List_Id": "<novedades.example.com>"},
        {"From": "MAILER-DAEMON@example.com"},
        {"From": "postmaster@example.com"},
        {"From": "noreply@example.com"},
        {"From": "no-reply@example.com"},
        {"From": "do-not-reply@example.com"},
        {"From": "donotreply@example.com"},
        {"From": "Rebotes <bounces@example.com>"},
        {"From": "bounce@example.com"},
    ],
)
def test_remitentes_automaticos_se_ignoran(headers):
    assert parse(raw_email(**headers)) == WebhookBatch(ignored=1)


def test_auto_submitted_no_es_un_correo_humano():
    assert len(parse(raw_email(Auto_Submitted="no")).messages) == 1


# ------------------------------------------------------------------ formato de salida
def test_formatter_lista_las_fuentes_y_agrega_el_pie():
    sources = (
        SourceLink("DNI", "https://www.argentina.gob.ar/dni"),
        SourceLink("Turnos", "https://www.argentina.gob.ar/turnos"),
    )
    (part,) = EmailFormatter().render(OutboundContent("Podés sacar turno online.", sources))
    assert part.text.startswith(
        "Podés sacar turno online.\n\nFuentes oficiales:\n"
        "- DNI: https://www.argentina.gob.ar/dni\n- Turnos: https://www.argentina.gob.ar/turnos\n\n--\n"
    )
    assert "Fuente: " not in part.text
    assert part.text.endswith("No incluyas datos personales en tus consultas.")


def test_formatter_sin_fuentes():
    (part,) = EmailFormatter().render(OutboundContent("  Respuesta corta.  "))
    assert part.text.startswith("Respuesta corta.\n\n--\nRespuesta automática")


# ------------------------------------------------------------------ envío SMTP
class FakeSmtp:
    """Reemplazo de smtplib.SMTP: registra la conversación y puede fallar en un paso."""

    def __init__(self) -> None:
        self.connections: list[tuple[str, int, float]] = []
        self.steps: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        self.fail: tuple[str, Exception] | None = None

    def __call__(self, host: str, port: int, timeout: float) -> FakeSmtp:
        self.connections.append((host, port, timeout))
        self._step("connect")
        return self

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _step(self, name: str) -> None:
        self.steps.append(name)
        if self.fail and self.fail[0] == name:
            raise self.fail[1]

    def ehlo(self) -> None:
        self._step("ehlo")

    def starttls(self, context: object = None) -> None:
        self._step("starttls")

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)
        self._step("login")

    def send_message(self, message: EmailMessage) -> None:
        self._step("send")
        self.sent.append(message)


@pytest.fixture
def smtp(monkeypatch) -> FakeSmtp:
    fake = FakeSmtp()
    monkeypatch.setattr(email_adapter.smtplib, "SMTP", fake)
    return fake


RECIPIENT = Recipient(
    Channel.EMAIL,
    "ana@example.com",
    "tramites@example.gob.ar",
    thread_ref="<abc123@example.com>",
    subject="Consulta DNI",
)


async def test_smtp_responde_en_el_mismo_hilo(smtp):
    result = await SmtpSender(CONFIG).send(RECIPIENT, RenderedPart("Podés sacar turno online."), SendContext(now=NOW))
    assert smtp.connections == [("smtp.example.test", 587, 20.0)]
    assert smtp.steps == ["connect", "ehlo", "starttls", "ehlo", "login", "send"]
    assert smtp.login_args == ("asistente", "smtp-pass")
    (message,) = smtp.sent
    sender = message["From"].addresses[0]
    assert (sender.display_name, sender.addr_spec) == ("Asistente de trámites", "tramites@example.gob.ar")
    assert message["To"] == "ana@example.com"
    assert message["Subject"] == "Re: Consulta DNI"
    assert message["Auto-Submitted"] == "auto-replied"
    assert message["In-Reply-To"] == "<abc123@example.com>"
    assert message["References"] == "<abc123@example.com>"
    assert message["Message-ID"].endswith("@example.gob.ar>")
    assert message.get_content() == "Podés sacar turno online.\n"
    assert result == SendResult(str(message["Message-ID"]))


@pytest.mark.parametrize(
    ("subject", "expected"), [("RE: Turno", "RE: Turno"), ("re: turno", "re: turno"), (None, "Re: Tu consulta")]
)
async def test_smtp_asunto(smtp, subject, expected):
    recipient = replace(RECIPIENT, subject=subject, thread_ref=None)
    await SmtpSender(CONFIG).send(recipient, RenderedPart("Hola"), SendContext(now=NOW))
    (message,) = smtp.sent
    assert message["Subject"] == expected
    assert message["In-Reply-To"] is None


async def test_smtp_sin_starttls_ni_usuario(smtp):
    config = replace(CONFIG, smtp_starttls=False, smtp_username="", smtp_port=25)
    await SmtpSender(config).send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert smtp.connections == [("smtp.example.test", 25, 20.0)]
    assert smtp.steps == ["connect", "ehlo", "send"]


async def test_smtp_no_configurado(smtp):
    with pytest.raises(DeliveryError) as caught:
        await SmtpSender(replace(CONFIG, smtp_host="")).send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert (caught.value.kind, caught.value.code) == (FailureKind.AUTH, "smtp_not_configured")
    assert smtp.connections == []


@pytest.mark.parametrize(
    ("step", "error", "kind", "code"),
    [
        ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed"), FailureKind.AUTH, "535"),
        (
            "send",
            smtplib.SMTPRecipientsRefused({"ana@example.com": (550, b"5.1.1 User unknown")}),
            FailureKind.PERMANENT,
            "recipient_refused",
        ),
        ("send", smtplib.SMTPDataError(451, b"4.3.0 Try again later"), FailureKind.RETRYABLE, "451"),
        ("send", smtplib.SMTPDataError(554, b"5.7.1 Rejected"), FailureKind.PERMANENT, "554"),
        ("ehlo", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), FailureKind.RETRYABLE, "smtp"),
        ("connect", ConnectionRefusedError("refused"), FailureKind.RETRYABLE, "smtp"),
        ("connect", TimeoutError("timed out"), FailureKind.RETRYABLE, "smtp"),
    ],
    ids=["auth", "destinatario", "4xx", "5xx", "desconexion", "conexion-rechazada", "timeout"],
)
async def test_smtp_clasifica_errores(smtp, step, error, kind, code):
    smtp.fail = (step, error)
    with pytest.raises(DeliveryError) as caught:
        await SmtpSender(CONFIG).send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert (caught.value.kind, caught.value.code) == (kind, code)
    assert "smtp-pass" not in str(caught.value)


def test_adaptador_completo():
    adapter = build_email_adapter(CONFIG)
    assert adapter.channel is Channel.EMAIL
    assert adapter.accepts_webhooks
    assert adapter.typing is None
    assert isinstance(adapter.formatter, EmailFormatter)


def test_utc_en_las_fechas_del_correo():
    # Una zona distinta de UTC se conserva como hora aware equivalente.
    sent_at = parse(raw_email(Date="Mon, 16 Nov 2026 09:00:00 -0300")).messages[0].sent_at
    assert sent_at.astimezone(UTC) == NOW
