"""Entradas mal formadas de los proveedores que no deben romper un webhook ni un reintento."""

from __future__ import annotations

import pytest

from iamia.channels.models import FailureKind
from iamia.integrations.http import parse_retry_after
from iamia.integrations.meta.common import meta_error

from .test_email import parse as parse_email
from .test_email import raw_email
from .test_sms import INBOUND
from .test_sms import parse as parse_sms


@pytest.mark.parametrize("value", ["²", "١٢", "abc", "", None])
def test_invalid_retry_after_is_ignored(value: str | None) -> None:
    assert parse_retry_after(value) is None


def test_numeric_retry_after() -> None:
    assert parse_retry_after(" 12 ") == 12.0


def test_meta_429_without_error_code_still_waits() -> None:
    error = meta_error(429, {}, {})
    assert error.kind is FailureKind.RATE_LIMITED and error.retry_after == 60.0


@pytest.mark.parametrize("value", ["abc", "²", "-1", "99999999"])
def test_invalid_num_media_does_not_break_the_sms_webhook(value: str) -> None:
    (message,) = parse_sms({**INBOUND, "NumMedia": value}).messages
    assert len(message.attachments) <= 10  # Twilio admite hasta 10 adjuntos


def test_html_email_without_styles_and_with_entities() -> None:
    body = (
        "<html><head><style>p { color: red; }</style></head>"
        "<body><p>Hola&nbsp;equipo &amp; gracias</p><script>alert(1)</script></body></html>"
    )
    (message,) = parse_email(raw_email(body, subtype="html")).messages
    assert message.text is not None
    assert "color" not in message.text and "alert" not in message.text
    assert "Hola equipo & gracias" in message.text.replace("\N{NO-BREAK SPACE}", " ")
