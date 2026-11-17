"""Tokens OAuth 2.0 de X: renovación coordinada, rotación y PKCE (iamia/integrations/x/tokens.py)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from datetime import datetime, timedelta

import httpx
import pytest

from iamia.channels.models import Channel, DeliveryError, FailureKind
from iamia.integrations.http import ProviderHttp
from iamia.integrations.x import tokens as x_tokens
from iamia.integrations.x.tokens import (
    X_SCOPES,
    MemoryTokenStore,
    OAuthClient,
    RefreshingAccessToken,
    StaticAccessToken,
    StoredToken,
    build_authorization,
    exchange_code,
    request_token,
)

from .support import NOW, basic_auth

CLIENT = OAuthClient(client_id="client-123", client_secret="client-secret")
REDIRECT_URI = "https://iamia.example.test/x/callback"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock(NOW)
    monkeypatch.setattr(x_tokens, "utcnow", clock)
    return clock


def token_reply(access: str = "access-2", refresh: str | None = "refresh-2", expires_in: object = 7200):
    body: dict[str, object] = {"token_type": "bearer", "access_token": access, "scope": " ".join(X_SCOPES)}
    if refresh is not None:
        body["refresh_token"] = refresh
    if expires_in is not None:
        body["expires_in"] = expires_in
    return httpx.Response(200, json=body)


async def store_with(expires_in: timedelta, access: str = "access-1", refresh: str = "refresh-1") -> MemoryTokenStore:
    store = MemoryTokenStore()
    await store.save("x", StoredToken(access, refresh, NOW + expires_in))
    return store


def provider(store: MemoryTokenStore, api) -> RefreshingAccessToken:
    return RefreshingAccessToken(store, CLIENT, api.http(Channel.X))


# ------------------------------------------------------------------ token fijo
async def test_static_access_token():
    token = StaticAccessToken("test-token")
    assert await token.access_token() == "test-token"
    assert await token.invalidate("test-token") is False  # no se puede renovar: no vale la pena reintentar


async def test_static_access_token_vacio():
    with pytest.raises(DeliveryError) as caught:
        await StaticAccessToken("").access_token()
    assert (caught.value.kind, caught.value.code) == (FailureKind.AUTH, "x_no_token")


# ------------------------------------------------------------------ renovación
async def test_usa_el_token_guardado_mientras_esta_vigente(fake_api, clock):
    api = fake_api()  # sin respuestas: cualquier pedido a X hace fallar la prueba
    tokens = provider(await store_with(timedelta(hours=1)), api)
    assert await tokens.access_token() == "access-1"
    assert await tokens.access_token() == "access-1"
    assert api.requests == []


async def test_sin_autorizacion_previa(fake_api, clock):
    with pytest.raises(DeliveryError) as caught:
        await provider(MemoryTokenStore(), fake_api()).access_token()
    assert (caught.value.kind, caught.value.code) == (FailureKind.AUTH, "x_unauthorized")


@pytest.mark.parametrize(
    ("expires_in", "refreshes"),
    [(timedelta(minutes=6), False), (timedelta(minutes=5), True), (timedelta(0), True), (-timedelta(hours=1), True)],
)
async def test_renueva_cerca_del_vencimiento(fake_api, clock, expires_in, refreshes):
    api = fake_api(token_reply())
    tokens = provider(await store_with(expires_in), api)
    assert await tokens.access_token() == ("access-2" if refreshes else "access-1")
    assert len(api.requests) == int(refreshes)


async def test_renovacion_guarda_enseguida_el_refresh_token_nuevo(fake_api, clock):
    store = await store_with(timedelta(minutes=1))
    api = fake_api(token_reply())
    assert await provider(store, api).access_token() == "access-2"
    request = api.last
    assert str(request.url) == "https://api.x.com/2/oauth2/token"
    assert request.headers["authorization"] == basic_auth("client-123", "client-secret")
    assert api.form() == {"grant_type": "refresh_token", "refresh_token": "refresh-1", "client_id": "client-123"}
    # El refresh token es de un solo uso: el nuevo queda guardado antes de usar el access token.
    assert await store.load("x") == StoredToken("access-2", "refresh-2", NOW + timedelta(seconds=7200))


async def test_cada_renovacion_usa_el_ultimo_refresh_token(fake_api, clock):
    store = await store_with(timedelta(minutes=1))
    api = fake_api(token_reply("access-2", "refresh-2"), token_reply("access-3", "refresh-3"))
    tokens = provider(store, api)
    assert await tokens.access_token() == "access-2"
    clock.now += timedelta(hours=2)  # vence el token renovado
    assert await tokens.access_token() == "access-3"
    assert [api.form(index)["refresh_token"] for index in range(2)] == ["refresh-1", "refresh-2"]
    assert (await store.load("x")).refresh_token == "refresh-3"


async def test_respuesta_sin_refresh_token_conserva_el_anterior(fake_api, clock):
    store = await store_with(timedelta(minutes=1))
    await provider(store, fake_api(token_reply(refresh=None, expires_in="3600"))).access_token()
    assert await store.load("x") == StoredToken("access-2", "refresh-1", NOW + timedelta(seconds=3600))


async def test_llamadas_concurrentes_renuevan_una_sola_vez(fake_api, clock):
    async def slow_token_endpoint(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)  # mientras tanto llegan los demás pedidos
        return token_reply()

    api = fake_api(slow_token_endpoint)
    tokens = provider(await store_with(timedelta(minutes=1)), api)
    assert await asyncio.gather(*(tokens.access_token() for _ in range(10))) == ["access-2"] * 10
    assert len(api.requests) == 1


async def test_dos_procesos_con_el_mismo_almacen_renuevan_una_sola_vez(fake_api, clock):
    async def slow_token_endpoint(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return token_reply()

    api = fake_api(slow_token_endpoint)
    store = await store_with(timedelta(minutes=1))
    first, second = provider(store, api), provider(store, api)
    assert await asyncio.gather(first.access_token(), second.access_token()) == ["access-2", "access-2"]
    assert len(api.requests) == 1  # el segundo relee el almacén bajo el lock y usa el token nuevo


async def test_relee_el_almacen_despues_de_tomar_el_lock(fake_api, clock):
    store = await store_with(timedelta(minutes=1))
    api = fake_api()
    tokens = provider(store, api)
    async with store.lock("x"):  # otro proceso está renovando
        pending = asyncio.create_task(tokens.access_token())
        for _ in range(3):
            await asyncio.sleep(0)
        assert not pending.done()  # espera el lock
        await store.save("x", StoredToken("access-otro", "refresh-otro", NOW + timedelta(hours=2)))
    assert await pending == "access-otro"
    assert api.requests == []


async def test_token_rechazado_fuerza_la_renovacion(fake_api, clock):
    store = await store_with(timedelta(hours=1))
    api = fake_api(token_reply())
    tokens = provider(store, api)
    assert await tokens.access_token() == "access-1"
    assert await tokens.invalidate("access-1") is True
    assert await tokens.access_token() == "access-2"  # aunque el guardado no venció, X lo rechazó
    assert api.form()["refresh_token"] == "refresh-1"


async def test_token_rechazado_pero_otro_proceso_ya_renovo(fake_api, clock):
    store = await store_with(timedelta(hours=1))
    api = fake_api()
    tokens = provider(store, api)
    await tokens.access_token()
    await tokens.invalidate("access-1")
    await store.save("x", StoredToken("access-otro", "refresh-otro", NOW + timedelta(hours=2)))
    assert await tokens.access_token() == "access-otro"
    assert api.requests == []


async def test_invalidar_un_token_viejo_no_descarta_el_actual(fake_api, clock):
    api = fake_api()
    tokens = provider(await store_with(timedelta(hours=1)), api)
    await tokens.access_token()
    assert await tokens.invalidate("access-viejo") is True
    assert await tokens.access_token() == "access-1"
    assert api.requests == []


@pytest.mark.parametrize(
    ("response", "kind", "code"),
    [
        (
            httpx.Response(
                400, json={"error": "invalid_request", "error_description": "Value passed for the token was invalid."}
            ),
            FailureKind.AUTH,
            "invalid_request",
        ),
        (httpx.Response(401, json={"error": "unauthorized_client"}), FailureKind.AUTH, "unauthorized_client"),
        (httpx.Response(429, json={}), FailureKind.RATE_LIMITED, "429"),
        (httpx.Response(503, text="Service Unavailable"), FailureKind.RETRYABLE, "503"),
        (httpx.Response(200, json={"token_type": "bearer"}), FailureKind.RETRYABLE, "200"),
    ],
    ids=["400", "401", "429", "503", "200-sin-token"],
)
async def test_errores_al_renovar_no_tocan_el_almacen(fake_api, clock, response, kind, code):
    store = await store_with(timedelta(minutes=1))
    with pytest.raises(DeliveryError) as caught:
        await provider(store, fake_api(response)).access_token()
    assert (caught.value.kind, caught.value.code) == (kind, code)
    assert await store.load("x") == StoredToken("access-1", "refresh-1", NOW + timedelta(minutes=1))


async def test_reintenta_la_renovacion_despues_de_un_error(fake_api, clock):
    api = fake_api(httpx.Response(503), token_reply())
    tokens = provider(await store_with(timedelta(minutes=1)), api)
    with pytest.raises(DeliveryError):
        await tokens.access_token()
    assert await tokens.access_token() == "access-2"


async def test_cliente_publico_sin_secreto(fake_api, clock):
    api = fake_api(token_reply())
    public = OAuthClient(client_id="client-123", client_secret="")
    await request_token(api.http(Channel.X), public, {"grant_type": "refresh_token", "refresh_token": "refresh-1"})
    assert "authorization" not in api.last.headers
    assert api.form()["client_id"] == "client-123"


async def test_aclose_cierra_el_cliente_http():
    http = ProviderHttp(Channel.X, timeout=1.0)
    await RefreshingAccessToken(MemoryTokenStore(), CLIENT, http).aclose()
    assert http._client.is_closed


# ------------------------------------------------------------------ autorización inicial (PKCE)
def test_build_authorization_pkce_s256():
    pkce = build_authorization(CLIENT, REDIRECT_URI)
    url = httpx.URL(pkce.url)
    assert (url.scheme, url.host, url.path) == ("https", "x.com", "/i/oauth2/authorize")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode()).digest()).rstrip(b"=").decode()
    assert dict(url.params) == {
        "response_type": "code",
        "client_id": "client-123",
        "redirect_uri": REDIRECT_URI,
        "scope": "dm.read dm.write tweet.read users.read offline.access",
        "state": pkce.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    assert 43 <= len(pkce.verifier) <= 128  # RFC 7636
    assert re.fullmatch(r"[A-Za-z0-9\-._~]+", pkce.verifier)
    assert len(pkce.state) >= 32


def test_build_authorization_genera_valores_nuevos_y_acepta_otros_scopes():
    first = build_authorization(CLIENT, REDIRECT_URI)
    second = build_authorization(CLIENT, REDIRECT_URI, scopes=("dm.read", "offline.access"))
    assert first.state != second.state
    assert first.verifier != second.verifier
    assert httpx.URL(second.url).params["scope"] == "dm.read offline.access"


async def test_exchange_code(fake_api, clock):
    api = fake_api(token_reply("access-1", "refresh-1", expires_in="3600"))
    verifier = "v" * 64
    token = await exchange_code(
        api.http(Channel.X), CLIENT, code="auth-code", verifier=verifier, redirect_uri=REDIRECT_URI
    )
    assert token == StoredToken("access-1", "refresh-1", NOW + timedelta(seconds=3600))
    assert api.last.headers["authorization"] == basic_auth("client-123", "client-secret")
    assert api.form() == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "client_id": "client-123",
    }


@pytest.mark.parametrize("expires_in", [None, "dos horas", -5])
async def test_exchange_code_sin_expires_in_valido_asume_dos_horas(fake_api, clock, expires_in):
    api = fake_api(token_reply("access-1", "refresh-1", expires_in=expires_in))
    token = await exchange_code(api.http(Channel.X), CLIENT, code="c", verifier="v" * 43, redirect_uri=REDIRECT_URI)
    assert token.expires_at == NOW + timedelta(seconds=7200)


async def test_exchange_code_rechazado(fake_api, clock):
    api = fake_api(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(DeliveryError) as caught:
        await exchange_code(api.http(Channel.X), CLIENT, code="vencido", verifier="v" * 43, redirect_uri=REDIRECT_URI)
    assert (caught.value.kind, caught.value.code) == (FailureKind.AUTH, "invalid_grant")


async def test_memory_token_store_lock_exclusivo_por_proveedor():
    store = MemoryTokenStore()
    async with store.lock("x"):
        other = asyncio.create_task(_enter(store, "x"))
        independent = asyncio.create_task(_enter(store, "otro"))
        await asyncio.sleep(0.01)
        assert not other.done()
        assert independent.done()
    await asyncio.wait_for(other, timeout=1)


async def _enter(store: MemoryTokenStore, provider_name: str) -> None:
    async with store.lock(provider_name):
        pass
