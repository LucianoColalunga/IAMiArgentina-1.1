"""Piezas comunes a WhatsApp, Instagram y Messenger (plataforma de Meta).

- Suscripción: GET con hub.mode=subscribe, hub.verify_token y hub.challenge.
- Firma: cada POST trae X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app secret, cuerpo).
  Se calcula sobre los bytes tal cual llegaron: Meta firma el JSON con unicode escapado, así
  que volver a serializarlo cambiaría la firma.
- Errores: Meta pide decidir por `error.code` (y el subcódigo), no por el status HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from iamia.channels.contracts import HttpReply, WebhookRequest
from iamia.channels.models import Channel, DeliveryError, FailureKind
from iamia.core.errors import WebhookAuthError, WebhookPayloadError
from iamia.integrations.http import ProviderHttp, classify_status, parse_retry_after, safe_json
from iamia.security.signatures import constant_time_equals, hmac_sha256_hex, verify_meta_signature

DEFAULT_GRAPH_VERSION = "v27.0"
_MAX_CHALLENGE_LENGTH = 128

# Clasificación de errores de la Graph API (WhatsApp Cloud API y Send API de Messenger/Instagram).
_WINDOW_CLOSED_CODES = {131047}  # WhatsApp: pasaron más de 24 h desde el último mensaje del usuario
_WINDOW_CLOSED_SUBCODES = {2018278}  # Messenger/Instagram: mensaje fuera de la ventana permitida
_RATE_LIMITED_CODES = {4, 17, 32, 613, 80007, 130429, 131048, 131056}
_ACCOUNT_CODES = {3, 10, 102, 190, 200, 294, 368, 131031, 131042, 133010}  # token, permisos o cuenta
_RECIPIENT_CODES = {100, 551, 131008, 131009, 131021, 131026, 131049, 131050, 131051, 131062}
_TRANSIENT_CODES = {1, 2, 1200, 131000, 131016, 131057, 133004}
_DEFAULT_WAIT = {131056: 6.0, 130429: 1.0, 131048: 600.0}  # 131056: 1 mensaje cada 6 s por usuario


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def regain_access_seconds(headers: Mapping[str, str]) -> float | None:
    """Lee X-Business-Use-Case-Usage, donde Meta informa cuánto falta para recuperar el cupo."""
    raw = headers.get("x-business-use-case-usage")
    if not raw:
        return None
    try:
        usage = json.loads(raw)
    except ValueError:
        return None
    minutes = [
        item.get("estimated_time_to_regain_access")
        for items in (usage.values() if isinstance(usage, dict) else ())
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict)
    ]
    longest = max((value for value in minutes if isinstance(value, int | float)), default=0)
    return float(longest) * 60 if longest else None


def meta_error(status_code: int, data: Mapping[str, Any], headers: Mapping[str, str]) -> DeliveryError:
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = _as_int(error.get("code"))
    subcode = _as_int(error.get("error_subcode"))
    label = str(code if code is not None else status_code) + (f"/{subcode}" if subcode else "")
    detail = str(error.get("error_user_title") or error.get("message") or "error de la plataforma de Meta")[:200]
    retry_after = parse_retry_after(headers.get("retry-after")) or regain_access_seconds(headers)

    if code in _WINDOW_CLOSED_CODES or subcode in _WINDOW_CLOSED_SUBCODES:
        kind = FailureKind.WINDOW_CLOSED
    elif code in _RATE_LIMITED_CODES:
        kind = FailureKind.RATE_LIMITED
        retry_after = retry_after or _DEFAULT_WAIT.get(code, 60.0)
    elif code in _ACCOUNT_CODES:
        kind = FailureKind.AUTH
    elif code in _RECIPIENT_CODES:
        kind = FailureKind.PERMANENT
    elif code in _TRANSIENT_CODES:
        kind = FailureKind.RETRYABLE
    else:
        kind = classify_status(status_code) or FailureKind.RETRYABLE
        if kind is FailureKind.RATE_LIMITED:  # 429 sin código de Meta: igual se espera antes de reintentar
            retry_after = retry_after or 60.0
    return DeliveryError(kind, detail, code=label, retry_after=retry_after)


class MetaWebhookVerifier:
    """Desafío de suscripción y firma, iguales para los tres productos de Meta."""

    def __init__(self, *, app_secret: str, verify_token: str) -> None:
        self._app_secret = app_secret
        self._verify_token = verify_token

    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        query = request.query
        if query.get("hub.mode") != "subscribe" or not self._verify_token:
            return None
        if not constant_time_equals(query.get("hub.verify_token", ""), self._verify_token):
            return None
        challenge = query.get("hub.challenge", "")
        # Se devuelve tal cual: se acepta solo lo que manda Meta (alfanumérico corto) para no
        # convertir el endpoint en un reflector de contenido arbitrario.
        if not challenge or len(challenge) > _MAX_CHALLENGE_LENGTH or not challenge.isalnum():
            return None
        return HttpReply(challenge)

    def authenticate(self, request: WebhookRequest) -> None:
        if not verify_meta_signature(self._app_secret, request.body, request.headers.get("x-hub-signature-256")):
            raise WebhookAuthError("firma de Meta inválida")


def load_payload(body: bytes, expected_object: str) -> dict[str, Any] | None:
    """JSON del webhook, o None si es de otro producto de Meta (no es un error: se ignora)."""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise WebhookPayloadError("JSON inválido") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entry", []), list):
        raise WebhookPayloadError("estructura de webhook desconocida")
    if payload.get("object") != expected_object:
        return None
    return payload


class GraphClient:
    """Cliente mínimo de la Graph API para enviar mensajes.

    Si se configura el app secret, cada llamada lleva `appsecret_proof` (HMAC del token): así un
    token filtrado no sirve desde fuera del servidor si la app exige la prueba.
    """

    def __init__(
        self,
        channel: Channel,
        *,
        access_token: str,
        base_url: str = "https://graph.facebook.com",
        api_version: str = DEFAULT_GRAPH_VERSION,
        app_secret: str = "",
        appsecret_proof: bool = True,
        timeout: float = 15.0,
        http: ProviderHttp | None = None,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/{api_version.strip('/')}"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._params = (
            {"appsecret_proof": hmac_sha256_hex(app_secret, access_token.encode("utf-8"))}
            if appsecret_proof and app_secret and access_token
            else {}
        )
        self._http = http or ProviderHttp(channel, timeout)

    async def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._http.request(
            "POST", f"{self._base}/{path.lstrip('/')}", json=payload, headers=self._headers, params=self._params
        )
        data = safe_json(response)
        if response.status_code == 200 and "error" not in data:
            return data
        raise meta_error(response.status_code, data, response.headers)

    async def aclose(self) -> None:
        await self._http.aclose()
