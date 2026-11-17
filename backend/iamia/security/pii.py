"""Enmascarado de datos personales (Ley 25.326: minimización).

Se aplica antes de guardar un mensaje, antes de mandarlo al modelo y antes de usarlo como clave
de caché. Los operadores también ven el texto enmascarado: el asistente es informativo y no
necesita datos personales para responder.
"""

from __future__ import annotations

import re

# El orden importa: primero los patrones más largos o específicos.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("CBU", re.compile(r"(?<!\d)\d{22}(?!\d)")),
    ("TELEFONO", re.compile(r"\+54[ -]?9?[ -]?(?:\d[ -]?){9,10}\d(?!\d)")),
    ("TARJETA", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
    ("CUIL", re.compile(r"(?<!\d)(?:20|23|24|25|26|27|30|33|34)[-. ]?\d{8}[-. ]?\d(?!\d)")),
    ("TELEFONO", re.compile(r"(?<!\d)(?:11|15)[ -]\d{4}[ -]\d{4}(?!\d)")),
    # Celular o fijo con característica y sin separadores (10 dígitos: 1155556666, 3514445566).
    ("TELEFONO", re.compile(r"(?<!\d)\d{10}(?!\d)")),
    ("DNI", re.compile(r"(?<!\d)\d{1,2}\.?\d{3}\.?\d{3}(?!\d)")),
    ("PASAPORTE", re.compile(r"\b[A-Z]{3}\d{6}\b", re.IGNORECASE)),
]


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Reemplaza datos personales por marcadores ([DNI], [EMAIL], ...)."""
    found: list[str] = []
    for label, pattern in _PII_PATTERNS:
        text, count = pattern.subn(f"[{label}]", text)
        if count and label not in found:
            found.append(label)
    return text, found


def mask_address(address: str) -> str:
    """Versión parcial de una dirección para mostrar a operadores: +54 9 11 ****-1234, j***@dominio."""
    if "@" in address:
        user, _, domain = address.partition("@")
        return f"{user[:1]}***@{domain}"
    if len(address) <= 4:
        return "****"
    return f"{'*' * (len(address) - 4)}{address[-4:]}"
