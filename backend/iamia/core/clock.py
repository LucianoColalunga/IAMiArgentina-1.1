"""Hora del sistema en un solo lugar (y reemplazable en tests)."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def from_unix(seconds: float | int | str) -> datetime:
    """Convierte un timestamp Unix (segundos, como string o número) a datetime UTC."""
    return datetime.fromtimestamp(float(seconds), tz=UTC)


def from_unix_ms(milliseconds: float | int | str) -> datetime:
    return datetime.fromtimestamp(float(milliseconds) / 1000.0, tz=UTC)
