"""Tokens de usuario OAuth 2.0 de X.

El access token dura 2 horas y el refresh token es de un solo uso: cada renovación devuelve uno
nuevo e invalida el anterior. Si dos procesos renovaran a la vez, uno dejaría al otro con un
token muerto y el canal quedaría sin credenciales. Por eso:

- los tokens se guardan cifrados en PostgreSQL (TokenStore);
- la renovación se hace bajo un lock exclusivo entre procesos y, una vez tomado, se relee lo
  guardado: si otro proceso ya renovó, se usa ese token sin volver a llamar a X;
- el token nuevo se guarda inmediatamente después de recibirlo.

La autorización inicial (PKCE) la hace una persona una sola vez con `python -m iamia x-authorize`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from iamia.channels.models import DeliveryError, FailureKind
from iamia.core.clock import utcnow
from iamia.integrations.http import ProviderHttp, classify_status, safe_json

X_SCOPES = ("dm.read", "dm.write", "tweet.read", "users.read", "offline.access")


@dataclass(frozen=True)
class StoredToken:
    access_token: str
    refresh_token: str
    expires_at: datetime


class TokenStore(Protocol):
    async def load(self, provider: str) -> StoredToken | None: ...

    async def save(self, provider: str, token: StoredToken) -> None: ...

    def lock(self, provider: str) -> AbstractAsyncContextManager[None]: ...


class AccessTokenProvider(Protocol):
    async def access_token(self) -> str: ...

    async def invalidate(self, token: str) -> bool:
        """Descarta un token rechazado. True si vale la pena reintentar (se puede renovar)."""
        ...


class StaticAccessToken:
    """Token fijo (pruebas o integraciones de corta duración). No se renueva."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self) -> str:
        if not self._token:
            raise DeliveryError(FailureKind.AUTH, "no hay token de usuario de X configurado", code="x_no_token")
        return self._token

    async def invalidate(self, token: str) -> bool:
        return False


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    token_url: str = "https://api.x.com/2/oauth2/token"  # noqa: S105 - URL pública
    authorize_url: str = "https://x.com/i/oauth2/authorize"


def _token_from_response(data: dict[str, object], previous_refresh: str = "") -> StoredToken:
    expires_in = data.get("expires_in")
    seconds = int(expires_in) if isinstance(expires_in, int | str) and str(expires_in).isdigit() else 7200
    return StoredToken(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token") or previous_refresh),
        expires_at=utcnow() + timedelta(seconds=seconds),
    )


async def request_token(http: ProviderHttp, client: OAuthClient, form: dict[str, str]) -> dict[str, object]:
    auth = (client.client_id, client.client_secret) if client.client_secret else None
    response = await http.request("POST", client.token_url, data={**form, "client_id": client.client_id}, auth=auth)
    data = safe_json(response)
    if response.status_code == 200 and data.get("access_token"):
        return data
    kind = FailureKind.AUTH if response.status_code in (400, 401) else classify_status(response.status_code)
    raise DeliveryError(
        kind or FailureKind.RETRYABLE,
        "X rechazó el pedido de token",
        code=str(data.get("error") or response.status_code),
    )


class RefreshingAccessToken:
    def __init__(
        self,
        store: TokenStore,
        client: OAuthClient,
        http: ProviderHttp,
        *,
        provider: str = "x",
        refresh_margin: timedelta = timedelta(minutes=5),
    ) -> None:
        self._store = store
        self._client = client
        self._http = http
        self._provider = provider
        self._margin = refresh_margin
        self._cached: StoredToken | None = None
        self._local = asyncio.Lock()

    def _expiring(self, token: StoredToken) -> bool:
        return token.expires_at - utcnow() <= self._margin

    async def access_token(self) -> str:
        token = self._cached
        if token is None or self._expiring(token):
            async with self._local:  # un solo intento por proceso; el lock global coordina procesos
                token = self._cached
                if token is None or self._expiring(token):
                    token = await self._load_or_refresh()
                    self._cached = token
        return token.access_token

    async def invalidate(self, token: str) -> bool:
        if self._cached and self._cached.access_token == token:
            self._cached = StoredToken(token, self._cached.refresh_token, utcnow())  # fuerza renovar
        return True

    async def _load_or_refresh(self) -> StoredToken:
        async with self._store.lock(self._provider):
            current = await self._store.load(self._provider)
            if current is None:
                raise DeliveryError(
                    FailureKind.AUTH,
                    "X no está autorizado: ejecutar `python -m iamia x-authorize`",
                    code="x_unauthorized",
                )
            cached = self._cached
            rejected = cached is not None and cached.access_token == current.access_token and self._expiring(cached)
            if not self._expiring(current) and not rejected:
                return current  # otro proceso ya lo renovó
            data = await request_token(
                self._http, self._client, {"grant_type": "refresh_token", "refresh_token": current.refresh_token}
            )
            renewed = _token_from_response(data, current.refresh_token)
            await self._store.save(self._provider, renewed)
            return renewed

    async def aclose(self) -> None:
        await self._http.aclose()


# ----------------------------------------------------------------- autorización inicial (PKCE)
@dataclass(frozen=True)
class PkceRequest:
    url: str
    state: str
    verifier: str


def build_authorization(client: OAuthClient, redirect_uri: str, scopes: tuple[str, ...] = X_SCOPES) -> PkceRequest:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return PkceRequest(f"{client.authorize_url}?{query}", state, verifier)


async def exchange_code(
    http: ProviderHttp, client: OAuthClient, *, code: str, verifier: str, redirect_uri: str
) -> StoredToken:
    data = await request_token(
        http,
        client,
        {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )
    return _token_from_response(data)


class MemoryTokenStore:
    """Almacén en memoria para pruebas (un solo proceso)."""

    def __init__(self) -> None:
        self._tokens: dict[str, StoredToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, provider: str) -> StoredToken | None:
        return self._tokens.get(provider)

    async def save(self, provider: str, token: StoredToken) -> None:
        self._tokens[provider] = token

    def lock(self, provider: str) -> AbstractAsyncContextManager[None]:
        return _LockContext(self._locks.setdefault(provider, asyncio.Lock()))


class _LockContext:
    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()
