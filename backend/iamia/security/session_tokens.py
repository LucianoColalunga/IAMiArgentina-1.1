"""Tokens de sesión del chat web.

Formato: "<id de sesión>.<vencimiento unix>.<firma>". La firma es HMAC-SHA256 con un secreto
del servidor, así cualquier réplica de la API valida el token sin consultar una base.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from iamia.core.errors import Unauthorized

_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")


class SessionTokens:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        if len(secret) < 32:
            raise ValueError("WEBCHAT_TOKEN_SECRET debe tener al menos 32 caracteres")
        self._secret = secret.encode("utf-8")
        self._ttl = ttl_seconds

    @staticmethod
    def new_session_id() -> str:
        return secrets.token_hex(16)

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def issue(self, session_id: str, now: float | None = None) -> tuple[str, int]:
        if not _SESSION_ID.match(session_id):
            raise ValueError("id de sesión inválido")
        expires = int((now if now is not None else time.time()) + self._ttl)
        payload = f"{session_id}.{expires}"
        return f"{payload}.{self._sign(payload)}", expires

    def verify(self, token: str, now: float | None = None) -> str:
        parts = token.split(".")
        if len(parts) != 3 or not _SESSION_ID.match(parts[0]) or not parts[1].isdigit():
            raise Unauthorized("token de sesión inválido")
        payload = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(parts[2].encode("ascii"), self._sign(payload).encode("ascii")):
            raise Unauthorized("token de sesión inválido")
        if int(parts[1]) < (now if now is not None else time.time()):
            raise Unauthorized("la sesión venció")
        return parts[0]
