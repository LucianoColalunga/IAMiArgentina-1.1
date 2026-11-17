"""Validación de firmas de webhooks.

Cada plataforma firma distinto, pero todas se validan sobre el cuerpo crudo (bytes tal cual
llegaron, antes de parsear JSON) y con comparación en tiempo constante.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hmac_sha256_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def hmac_sha256_b64(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


# ------------------------------------------------------------------------- Meta
def meta_signature(app_secret: str, body: bytes) -> str:
    """Valor esperado del header X-Hub-Signature-256 (WhatsApp, Instagram, Messenger)."""
    return f"sha256={hmac_sha256_hex(app_secret, body)}"


def verify_meta_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header:
        return False
    return constant_time_equals(header.strip(), meta_signature(app_secret, body))


# ------------------------------------------------------------------------- X
def x_crc_response_token(consumer_secret: str, crc_token: str) -> str:
    """Respuesta al desafío CRC de la Account Activity API de X."""
    return f"sha256={hmac_sha256_b64(consumer_secret, crc_token.encode('utf-8'))}"


def x_signature(consumer_secret: str, body: bytes) -> str:
    return f"sha256={hmac_sha256_b64(consumer_secret, body)}"


def verify_x_signature(consumer_secret: str, body: bytes, header: str | None) -> bool:
    if not consumer_secret or not header:
        return False
    return constant_time_equals(header.strip(), x_signature(consumer_secret, body))


# ------------------------------------------------------------------------- Twilio
def twilio_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    """HMAC-SHA1 de la URL completa + parámetros POST ordenados por nombre (nombre y valor pegados)."""
    payload = url + "".join(f"{name}{value}" for name, value in sorted(params, key=lambda item: (item[0], item[1])))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]], header: str | None) -> bool:
    if not auth_token or not header or not url:
        return False
    return constant_time_equals(header.strip(), twilio_signature(auth_token, url, params))


# ------------------------------------------------------------------------- firma propia con timestamp
def timestamped_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Firma para integraciones propias (correo entrante): v1=HMAC-SHA256(secreto, "ts." + cuerpo)."""
    return f"v1={hmac_sha256_hex(secret, timestamp.encode('ascii') + b'.' + body)}"


def verify_timestamped_signature(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float,
    tolerance_seconds: int = 300,
) -> bool:
    """Valida firma y frescura: un pedido capturado no se puede reenviar pasada la tolerancia."""
    if not secret or not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > tolerance_seconds:
        return False
    return constant_time_equals(signature.strip(), timestamped_signature(secret, timestamp, body))
