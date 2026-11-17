"""Cliente HTTP común a los adaptadores: timeouts, métrica de latencia y clasificación de errores.

Importante: varias APIs llevan credenciales en la URL (Telegram pone el token del bot en la
ruta). Por eso los errores se describen solo con el tipo de excepción y nunca con la URL.
"""

from __future__ import annotations

import json
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from iamia.channels.models import Channel, DeliveryError, FailureKind
from iamia.core.clock import utcnow
from iamia.core.metrics import DELIVERY_SECONDS


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():  # isdigit solo acepta "²" y otros dígitos Unicode
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
    except (TypeError, ValueError):
        return None


def classify_status(status_code: int) -> FailureKind | None:
    """Clasificación genérica por código HTTP (cada adaptador la refina con sus códigos propios)."""
    if status_code < 400:
        return None
    if status_code == 429:
        return FailureKind.RATE_LIMITED
    if status_code in (401, 403):
        return FailureKind.AUTH
    if status_code == 408 or status_code >= 500:
        return FailureKind.RETRYABLE
    return FailureKind.PERMANENT


def safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class ProviderHttp:
    def __init__(self, channel: Channel, timeout: float = 15.0, client: httpx.AsyncClient | None = None) -> None:
        self._channel = channel
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        try:
            return await self._client.request(method, url, timeout=self._timeout, **kwargs)
        except httpx.TimeoutException as exc:
            raise DeliveryError(FailureKind.RETRYABLE, "timeout de la plataforma", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise DeliveryError(FailureKind.RETRYABLE, f"error de red: {type(exc).__name__}", code="network") from exc
        finally:
            DELIVERY_SECONDS.labels(self._channel.value).observe(time.perf_counter() - started)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
