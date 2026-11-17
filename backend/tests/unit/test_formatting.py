"""Formato de salida compartido por los canales: partes, fuentes y SMS en GSM-7."""

from __future__ import annotations

import pytest

from iamia.channels.formatting import (
    PlainTextFormatter,
    compose_text,
    fit_sms,
    gsm7_length,
    sms_segments,
    split_text,
    split_text_bytes,
    to_gsm7,
)
from iamia.channels.models import OutboundContent, SourceLink

DNI = SourceLink("DNI", "https://www.argentina.gob.ar/dni")


def test_split_text_prefers_paragraphs() -> None:
    text = "Primer párrafo con información.\n\nSegundo párrafo con más información sobre el trámite."
    assert split_text(text, 60) == [
        "Primer párrafo con información.",
        "Segundo párrafo con más información sobre el trámite.",
    ]


def test_split_text_never_exceeds_the_limit_or_loses_words() -> None:
    text = "palabra " * 500
    parts = split_text(text, 100)
    assert all(len(part) <= 100 for part in parts)
    assert " ".join(parts).split() == text.split()
    assert split_text("a" * 250, 100) == ["a" * 100, "a" * 100, "a" * 50]  # sin espacios se corta igual
    with pytest.raises(ValueError):
        split_text("hola", 0)


def test_split_text_bytes_counts_utf8() -> None:
    text = "ñandú " * 400  # cada "ñ" y "ú" ocupan 2 bytes
    parts = split_text_bytes(text, 1000)
    assert len(parts) > 1 and all(len(part.encode("utf-8")) <= 1000 for part in parts)
    assert " ".join(parts).split() == text.split()
    with pytest.raises(ValueError):
        split_text_bytes("hola", 3)


def test_compose_text_adds_the_source_once() -> None:
    content = OutboundContent("Se saca con turno.", (DNI,))
    assert compose_text(content) == "Se saca con turno.\n\nFuente: https://www.argentina.gob.ar/dni"
    cited = OutboundContent("Se saca con turno. Fuente: https://www.argentina.gob.ar/dni", (DNI,))
    assert compose_text(cited) == cited.text
    assert compose_text(content, include_sources=False) == "Se saca con turno."


def test_plain_text_formatter() -> None:
    parts = PlainTextFormatter(50).render(OutboundContent("uno dos tres " * 10, (DNI,)))
    assert len(parts) > 1 and all(len(part.text) <= 50 for part in parts)
    assert parts[-1].text.endswith("https://www.argentina.gob.ar/dni")
    by_bytes = PlainTextFormatter(1000, max_bytes=40).render(OutboundContent("ñandú " * 30))
    assert all(len(part.text.encode("utf-8")) <= 40 for part in by_bytes)


def test_gsm7_keeps_spanish_readable() -> None:
    assert to_gsm7("¿Cómo está? “Sí” — Ñandú…") == '¿Como esta? "Si" - Ñandu...'
    assert to_gsm7("café") == "café"  # la "é" existe en GSM-7
    assert to_gsm7("hola " + chr(0x1F600)) == "hola ?"


def test_sms_segments_and_fit() -> None:
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    assert sms_segments("a" * 307) == 3
    assert gsm7_length("€") == 2  # los caracteres extendidos ocupan dos posiciones
    fitted = fit_sms("palabra " * 200, 3)
    assert gsm7_length(fitted) <= 459 and fitted.endswith("...")
    assert fit_sms("corto", 1) == "corto"
