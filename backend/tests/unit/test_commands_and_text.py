"""Comandos que escribe la persona y utilidades de texto compartidas."""

from __future__ import annotations

import pytest

from iamia.conversations.commands import (
    LINK_ALPHABET,
    Command,
    ParsedCommand,
    format_link_code,
    new_link_code,
    parse_command,
)
from iamia.core.text import clean_text, fold, normalize_for_cache, stable_hash, truncate


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("/start", Command.START),
        ("AYUDA", Command.HELP),
        ("Menú", Command.HELP),
        ("humano", Command.HUMAN),
        ("Quiero hablar con una persona", Command.HUMAN),
        ("necesito un asesor por favor", Command.HUMAN),
        ("hola, quisiera hablar con un operador real", Command.HUMAN),
        ("BORRAR", Command.ERASE),
        ("borrar confirmar", Command.ERASE_CONFIRM),
        ("BAJA", Command.STOP),
        ("alta", Command.RESUME),
        ("Privacidad", Command.PRIVACY),
        ("Hola", Command.GREETING),
        ("¡Holaaa!", Command.GREETING),
        ("Buenas tardes", Command.GREETING),
        ("hola, ¿qué tal?", Command.GREETING),
        ("Hola buen día", Command.GREETING),
    ],
)
def test_commands_are_recognized(text: str, command: Command) -> None:
    parsed = parse_command(text)
    assert parsed is not None and parsed.command is command


@pytest.mark.parametrize(
    "text",
    [
        "¿Cómo doy de baja la tarjeta?",
        "¿Una persona puede tramitar el DNI de otra?",
        "persona juridica",
        "quiero sacar un turno",
        "Hola, ¿cómo saco el DNI digital?",
        "buenas tardes quiero sacar un turno",
        "",
        None,
        "humano " * 10,
    ],
)
def test_questions_are_not_commands(text: str | None) -> None:
    assert parse_command(text) is None


def test_link_command_with_and_without_code() -> None:
    assert parse_command("VINCULAR") == ParsedCommand(Command.LINK, None)
    assert parse_command("vincular abc-234") == ParsedCommand(Command.LINK, "ABC234")
    assert parse_command("/vincular ABC 234") == ParsedCommand(Command.LINK, "ABC234")


def test_link_codes_use_an_unambiguous_alphabet() -> None:
    for _ in range(50):
        code = new_link_code()
        assert len(code) == 6 and set(code) <= set(LINK_ALPHABET)
    assert not set("01ILO") & set(LINK_ALPHABET)
    assert format_link_code("ABC234") == "ABC-234"


def test_clean_text_removes_invisible_and_control_characters() -> None:
    dirty = "Ho\N{ZERO WIDTH SPACE}la" + chr(0) + "  mundo\N{NO-BREAK SPACE}!\n\n\n\nchau"
    assert clean_text(dirty) == "Hola mundo !\n\nchau"
    assert clean_text(None) == ""


def test_fold_and_cache_key() -> None:
    assert fold("ÁÉÍ Ñandú") == "aei nandu"
    assert normalize_for_cache("¡Hola! ¿Cómo saco el DNI digital, por favor?") == "como saco el dni digital"
    assert normalize_for_cache("como saco el dni digital") == normalize_for_cache("COMO SACO EL DNI DIGITAL!!")


def test_truncate_and_hash() -> None:
    assert truncate("una frase bastante larga", 12) == "una frase…"
    assert truncate("corta", 12) == "corta"
    # El separador evita colisiones al concatenar partes.
    assert stable_hash("a", "bc") != stable_hash("ab", "c")
    assert stable_hash("x") == stable_hash("x")
