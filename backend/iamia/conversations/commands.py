"""Comandos que la persona puede escribir en cualquier canal.

Se reconocen solo si el mensaje completo es el comando (con o sin "/" y sin importar tildes ni
mayúsculas): "¿Cómo doy de baja la tarjeta?" es una consulta, "BAJA" es el comando.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

from iamia.core.text import fold


class Command(StrEnum):
    START = "start"
    HELP = "help"
    HUMAN = "human"
    PRIVACY = "privacy"
    ERASE = "erase"
    ERASE_CONFIRM = "erase_confirm"
    LINK = "link"
    STOP = "stop"
    RESUME = "resume"
    GREETING = "greeting"  # un saludo solo: se contesta con la bienvenida, sin pasar por el motor


_KEYWORDS: dict[str, Command] = {}
for _command, _words in {
    Command.START: ("start", "inicio", "comenzar", "empezar"),
    Command.HELP: ("ayuda", "help", "menu", "opciones"),
    Command.HUMAN: (
        "humano",
        "operador",
        "operadora",
        "agente",
        "persona",
        "hablar con una persona",
        "hablar con un humano",
        "hablar con un operador",
        "atencion humana",
    ),
    Command.PRIVACY: ("privacidad", "mis datos", "politica de privacidad"),
    Command.ERASE: ("borrar", "borrar mis datos", "eliminar mis datos", "olvidame"),
    Command.ERASE_CONFIRM: ("borrar confirmar", "confirmar borrado", "borrar confirmo"),
    Command.STOP: ("stop", "baja", "unsubscribe", "desuscribir", "stopall", "cancelar suscripcion"),
    Command.RESUME: ("alta", "unstop", "reanudar", "volver"),
}.items():
    for _word in _words:
        _KEYWORDS[_word] = _command

_CLEAN = re.compile(r"[^a-z0-9 ]+")
# Pedidos de atención humana dichos con naturalidad ("quiero hablar con una persona",
# "necesito un asesor por favor"). Tiene que ser todo el mensaje: una consulta que menciona a
# "una persona" ("¿una persona puede tramitar el DNI de otra?") no coincide.
_HUMAN_REQUEST = re.compile(
    r"^(?:(?:hola|buenas|buen dia|por favor)\s+)?"
    r"(?:(?:quiero|quisiera|necesito|me gustaria|puedo|podria|deseo)\s+)?"
    r"(?:(?:hablar|comunicarme|chatear)\s+)?(?:con\s+)?(?:una?\s+)?"
    r"(?:persona|humano|humana|operador|operadora|agente|asesor|asesora|representante)"
    r"(?:\s+(?:real|del equipo|por favor))*$"
)
# Un saludo solo ("Hola", "¡Buenas tardes!", "hola, ¿qué tal?") es lo primero que escribe mucha
# gente. Sin esto iría al motor y recibiría el aviso de "fuera de tema". "Hola, ¿cómo saco el DNI?"
# no coincide: tiene una consulta y va al motor como siempre.
_GREETING = re.compile(
    r"^(?:hola+|buenas|buen dia|buenos dias|buenas tardes|buenas noches)"
    r"(?:\s+(?:buenas|buen dia|buenos dias|buenas tardes|buenas noches|que tal|como estas|como va))?$"
)
_LINK = re.compile(r"^vincular(?:\s+([a-z0-9]{3})[\s-]?([a-z0-9]{3}))?$")
# Sin letras ni dígitos que se confunden (0/O, 1/I/L): el código se copia a mano entre canales.
LINK_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    command: Command
    argument: str | None = None


def parse_command(text: str | None) -> ParsedCommand | None:
    if not text or len(text) > 60:
        return None
    raw = fold(text).replace("-", " ").strip()
    normalized = " ".join(_CLEAN.sub(" ", raw.lstrip("/")).split())
    match = _LINK.match(normalized)
    if match:
        code = (match.group(1) or "") + (match.group(2) or "")
        return ParsedCommand(Command.LINK, code.upper() or None)
    command = _KEYWORDS.get(normalized)
    if command is None and _HUMAN_REQUEST.match(normalized):
        command = Command.HUMAN
    if command is None and _GREETING.match(normalized):
        command = Command.GREETING
    return ParsedCommand(command) if command else None


def new_link_code() -> str:
    return "".join(secrets.choice(LINK_ALPHABET) for _ in range(6))


def format_link_code(code: str) -> str:
    return f"{code[:3]}-{code[3:]}"
