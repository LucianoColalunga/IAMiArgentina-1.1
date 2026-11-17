"""API HTTP con un contenedor de dobles: cabeceras, errores, consultas, webhooks, chat web y consola."""

from __future__ import annotations

import asyncio
import re
import secrets
import socket
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import asyncpg
import fakeredis
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from redis.exceptions import ConnectionError as RedisConnectionError

from iamia.ai.engine import Answer, Outcome, Source
from iamia.ai.prompts import BUSY_MESSAGE, UNAVAILABLE_MESSAGE
from iamia.api.app import create_app
from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities, HttpReply, WebhookRequest
from iamia.channels.formatting import PlainTextFormatter
from iamia.channels.models import Channel, RenderedPart, SendContext, SendResult, WebhookBatch
from iamia.channels.registry import ChannelRegistry
from iamia.core.errors import ServiceBusy, WebhookAuthError, WebhookPayloadError
from iamia.integrations.webchat.adapter import WebchatPublisher, build_webchat_adapter
from iamia.messaging.memory import MemoryJobQueue
from iamia.security.auth import JwtVerifier, OidcConfig
from iamia.security.session_tokens import SessionTokens
from iamia.services.ingest import IngestResult
from iamia.settings import Settings
from tests.factories import inbound_message

SESSION_SECRET = secrets.token_urlsafe(32)  # uno nuevo en cada corrida
HEX_ID = re.compile(r"[0-9a-f]{32}")


# ----------------------------------------------------------------------------- dobles
@dataclass
class FakeEngine:
    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def answer(self, question: str, *, client_key: str) -> Answer:
        self.calls.append((question, client_key))
        if self.error is not None:
            raise self.error
        return Answer(
            "Se saca con turno.",
            Outcome.ANSWERED,
            (Source("DNI", "https://www.argentina.gob.ar/dni"),),
            kb_version="20261101000000",
            input_tokens=120,
            output_tokens=30,
            latency_ms=15,
        )


class FakeVerifier:
    def challenge(self, request: WebhookRequest) -> HttpReply | None:
        return HttpReply("desafio-ok") if request.query.get("token") == "correcto" else None

    def authenticate(self, request: WebhookRequest) -> None:
        if request.headers.get("x-firma") != "valida":
            raise WebhookAuthError("firma inválida")


class FakeParser:
    def parse(self, request: WebhookRequest) -> WebhookBatch:
        if request.body == b"roto":
            raise WebhookPayloadError("json inválido")
        return WebhookBatch(messages=(inbound_message("w-1"),))


class NullSender:
    async def send(self, recipient: object, part: RenderedPart, context: SendContext) -> SendResult:
        return SendResult(None)


@dataclass
class RecordingIngest:
    batches: list[tuple[WebhookBatch, str]] = field(default_factory=list)

    async def ingest(self, batch: WebhookBatch, *, trace_id: str = "") -> IngestResult:
        self.batches.append((batch, trace_id))
        return IngestResult(accepted=len(batch.messages))


@dataclass
class FakeWebchatRepo:
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    async def messages_after(self, session_id: str, after: int) -> list[dict[str, Any]]:
        return [message for message in self.messages.get(session_id, []) if message["seq"] > after]


class FakeContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        self.engine = FakeEngine()
        self.queue = MemoryJobQueue()
        self.ingest = RecordingIngest()
        self.webchat_repo = FakeWebchatRepo()
        telegram = ChannelAdapter(
            channel=Channel.TELEGRAM,
            capabilities=ChannelCapabilities(max_text_chars=4096),
            formatter=PlainTextFormatter(4096),
            sender=NullSender(),
            verifier=FakeVerifier(),
            parser=FakeParser(),
        )
        self.registry = ChannelRegistry([telegram, build_webchat_adapter(self.redis)])
        self.database_ok = True
        self.verifier: JwtVerifier | None = None
        self.closed = False

    async def ping_database(self) -> bool:
        return self.database_ok

    async def ping_redis(self) -> bool:
        return True

    def token_verifier(self) -> JwtVerifier | None:
        return self.verifier

    def session_tokens(self) -> SessionTokens:
        return SessionTokens(SESSION_SECRET, 3600)

    async def close(self) -> None:
        self.closed = True
        await self.redis.aclose()


@dataclass
class Api:
    client: httpx.AsyncClient
    container: FakeContainer


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "development",
        "edge_shared_secret": "",
        "public_base_url": "",
        "rate_limit_per_minute": 3,
        "webchat_long_poll_seconds": 0.3,
        "webchat_ui_enabled": False,
        "console_ui_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@asynccontextmanager
async def running(settings: Settings, configure: Callable[[FakeContainer], None] | None = None) -> AsyncIterator[Api]:
    holder: dict[str, FakeContainer] = {}

    async def factory(current: Settings) -> FakeContainer:
        holder["container"] = FakeContainer(current)
        if configure is not None:
            configure(holder["container"])
        return holder["container"]

    app = create_app(settings, factory=factory)  # type: ignore[arg-type]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield Api(client, holder["container"])
    assert holder["container"].closed  # el apagado ordenado cierra las conexiones


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    # Reloj fijo para los límites por ventana: la prueba no depende del segundo en que corre.
    monkeypatch.setattr("iamia.ai.limits.time", SimpleNamespace(time=lambda: 1_000_050.0))
    async with running(_settings()) as value:
        yield value


# ----------------------------------------------------------------------------- salud y cabeceras
async def test_liveness_and_security_headers(api: Api) -> None:
    response = await api.client.get("/healthz")
    assert response.status_code == 200 and response.json()["status"] == "ok"
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"].startswith("default-src 'none'")
    assert headers["referrer-policy"] == "no-referrer"
    assert HEX_ID.fullmatch(headers["x-request-id"])
    assert "strict-transport-security" not in headers  # HSTS solo en producción


async def test_request_id_is_propagated_only_when_safe(api: Api) -> None:
    ok = await api.client.get("/healthz", headers={"X-Request-ID": "abcdef12-3456"})
    assert ok.headers["x-request-id"] == "abcdef12-3456"
    unsafe = await api.client.get("/healthz", headers={"X-Request-ID": "<script>alert(1)</script>"})
    assert HEX_ID.fullmatch(unsafe.headers["x-request-id"])


async def test_readiness_reports_each_dependency(api: Api) -> None:
    assert (await api.client.get("/readyz")).status_code == 200
    api.container.database_ok = False
    response = await api.client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": False, "redis": True, "queue": True}}


async def test_metrics_endpoint(api: Api) -> None:
    await api.client.get("/healthz")
    response = await api.client.get("/metrics")
    assert response.status_code == 200 and "iamia_http_requests_total" in response.text


async def test_oversized_bodies_are_rejected(api: Api) -> None:
    declared = await api.client.post(
        "/v1/chat", content=b"x" * (3 * 1024 * 1024 + 1), headers={"content-type": "application/json"}
    )
    assert declared.status_code == 413 and declared.json()["detail"].startswith("El cuerpo")
    assert declared.headers["x-content-type-options"] == "nosniff"

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * (1024 * 1024)

    # Sin Content-Length (envío en partes) el corte ocurre mientras se lee el cuerpo.
    streamed = await api.client.post("/v1/chat", content=chunks(), headers={"content-type": "application/json"})
    assert streamed.status_code == 413
    webhook = await api.client.post("/webhooks/telegram", content=chunks(), headers={"x-firma": "valida"})
    assert webhook.status_code == 413
    assert api.container.ingest.batches == []


async def test_docs_only_outside_production() -> None:
    for environment, status in (("development", 200), ("production", 404)):
        app = create_app(_settings(app_env=environment))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/openapi.json")
            assert response.status_code == status
            assert ("strict-transport-security" in response.headers) is (environment == "production")


# ----------------------------------------------------------------------------- consulta directa
async def test_chat_answers_with_the_v1_schema(api: Api) -> None:
    response = await api.client.post("/v1/chat", json={"question": "¿Cómo saco el DNI?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Se saca con turno."
    assert body["sources"] == [{"title": "DNI", "url": "https://www.argentina.gob.ar/dni"}]
    assert body["usage"] == {"input_tokens": 120, "output_tokens": 30} and body["kb_version"] == "20261101000000"
    assert body["blocked"] is False and body["cached"] is False
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["x-cache"] == "MISS" and response.headers["cache-control"] == "private, no-store"
    assert api.container.engine.calls == [("¿Cómo saco el DNI?", "ip:127.0.0.1")]


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x", "extra": 1}, {"question": "x" * 4001}])
async def test_invalid_chat_requests(api: Api, payload: dict[str, Any]) -> None:
    response = await api.client.post("/v1/chat", json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": "El pedido no tiene el formato esperado."}
    assert api.container.engine.calls == []


async def test_chat_rate_limit_per_ip(api: Api) -> None:
    for _ in range(3):
        assert (await api.client.post("/v1/chat", json={"question": "hola"})).status_code == 200
    limited = await api.client.post("/v1/chat", json={"question": "hola"})
    assert limited.status_code == 429 and limited.headers["retry-after"] == "30"
    assert len(api.container.engine.calls) == 3


async def test_engine_errors_map_to_http(api: Api) -> None:
    api.container.engine.error = ServiceBusy("saturado", retry_after=7)
    busy = await api.client.post("/v1/chat", json={"question": "hola"})
    assert busy.status_code == 503 and busy.json()["detail"] == BUSY_MESSAGE and busy.headers["retry-after"] == "7"
    api.container.engine.error = RuntimeError("detalle interno que no se debe ver")
    failed = await api.client.post("/v1/chat", json={"question": "hola"})
    assert failed.status_code == 500 and failed.json()["detail"] == UNAVAILABLE_MESSAGE
    assert "detalle interno" not in failed.text


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.CannotConnectNowError("the database system is shutting down"),
        ConnectionResetError(104, "Connection reset by peer"),
        socket.gaierror(-2, "Name or service not known"),
        RedisConnectionError("Error 111 connecting to redis:6379"),
    ],
)
async def test_dependency_restarts_answer_503_so_platforms_retry(api: Api, error: Exception) -> None:
    api.container.engine.error = error
    response = await api.client.post("/v1/chat", json={"question": "hola"})
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert response.json()["detail"] == UNAVAILABLE_MESSAGE


async def test_edge_secret_is_required_when_configured() -> None:
    async with running(_settings(edge_shared_secret="borde-secreto")) as api:
        assert (await api.client.post("/v1/chat", json={"question": "hola"})).status_code == 401
        wrong = await api.client.post("/v1/chat", json={"question": "hola"}, headers={"X-Edge-Auth": "otro"})
        assert wrong.status_code == 401
        ok = await api.client.post("/v1/chat", json={"question": "hola"}, headers={"X-Edge-Auth": "borde-secreto"})
        assert ok.status_code == 200


# ----------------------------------------------------------------------------- webhooks
async def test_webhook_subscription_challenge(api: Api) -> None:
    ok = await api.client.get("/webhooks/telegram", params={"token": "correcto"})
    assert ok.status_code == 200 and ok.text == "desafio-ok"
    assert (await api.client.get("/webhooks/telegram", params={"token": "otro"})).status_code == 403
    assert (await api.client.get("/webhooks/whatsapp")).status_code == 404  # canal no habilitado
    assert (await api.client.get("/webhooks/inexistente")).status_code == 404
    assert (await api.client.get("/webhooks/webchat")).status_code == 404  # canal sin webhooks


async def test_webhook_events(api: Api) -> None:
    unsigned = await api.client.post("/webhooks/telegram", content=b"{}")
    assert unsigned.status_code == 401 and unsigned.json()["detail"] == "Firma inválida."
    broken = await api.client.post("/webhooks/telegram", content=b"roto", headers={"X-Firma": "valida"})
    assert broken.status_code == 400 and broken.json()["detail"] == "Contenido inválido."
    accepted = await api.client.post(
        "/webhooks/telegram", content=b"{}", headers={"X-Firma": "valida", "X-Request-ID": "traza-0001"}
    )
    assert accepted.status_code == 200 and accepted.text == "ok"
    assert len(api.container.ingest.batches) == 1  # los rechazados no llegaron a la ingesta
    batch, trace_id = api.container.ingest.batches[0]
    assert batch.messages[0].provider_message_id == "w-1" and trace_id == "traza-0001"


# ----------------------------------------------------------------------------- chat web
async def test_webchat_session_send_and_poll(api: Api) -> None:
    created = await api.client.post("/webchat/sessions")
    assert created.status_code == 200
    token = created.json()["token"]
    session_id = token.split(".")[0]
    auth = {"Authorization": f"Bearer {token}"}

    sent = await api.client.post(
        "/webchat/messages", json={"text": "¿Cómo saco el DNI?", "client_message_id": "abc-1"}, headers=auth
    )
    assert sent.status_code == 202 and sent.json() == {"accepted": True, "duplicated": False}
    message = api.container.ingest.batches[-1][0].messages[0]
    assert message.channel is Channel.WEBCHAT and message.sender_id == session_id
    assert message.provider_message_id == f"{session_id}:abc-1"

    api.container.webchat_repo.messages[session_id] = [{"seq": 1, "author": "bot", "text": "Hola"}]
    polled = await api.client.get("/webchat/messages", params={"after": 0, "wait": "false"}, headers=auth)
    assert polled.json() == {"messages": [{"seq": 1, "author": "bot", "text": "Hola"}]}
    later = await api.client.get("/webchat/messages", params={"after": 1, "wait": "false"}, headers=auth)
    assert later.json() == {"messages": []}


async def test_webchat_long_poll_times_out_quietly(api: Api) -> None:
    token = (await api.client.post("/webchat/sessions")).json()["token"]
    started = time.monotonic()
    response = await api.client.get("/webchat/messages", headers={"Authorization": f"Bearer {token}"})
    assert response.json() == {"messages": []} and 0.2 <= time.monotonic() - started < 5


async def test_webchat_long_poll_wakes_up_with_the_reply() -> None:
    async with running(_settings(webchat_long_poll_seconds=8)) as api:
        token = (await api.client.post("/webchat/sessions")).json()["token"]
        session_id = token.split(".")[0]

        async def reply_later() -> None:
            await asyncio.sleep(0.3)
            api.container.webchat_repo.messages[session_id] = [{"seq": 1, "author": "bot", "text": "Listo"}]
            await WebchatPublisher(api.container.redis).publish(session_id)

        started = time.monotonic()
        replier = asyncio.create_task(reply_later())
        response = await api.client.get("/webchat/messages", headers={"Authorization": f"Bearer {token}"})
        await replier
        assert response.json()["messages"][0]["text"] == "Listo"
        assert time.monotonic() - started < 4  # despertó por la notificación, no por el tiempo máximo


@pytest.mark.parametrize("header", [None, "Bearer", "Basic abc", "Bearer no-es-un-token"])
async def test_webchat_requires_a_valid_session(api: Api, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    response = await api.client.post("/webchat/messages", json={"text": "hola"}, headers=headers)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert api.container.ingest.batches == []


async def test_webchat_rejects_tokens_signed_by_someone_else(api: Api) -> None:
    forged, _ = SessionTokens("otro-secreto-de-al-menos-32-caracteres!!", 3600).issue(SessionTokens.new_session_id())
    response = await api.client.get("/webchat/messages", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_webchat_sessions_are_limited_per_ip() -> None:
    async with running(_settings(webchat_sessions_per_ip_per_hour=2)) as api:
        assert (await api.client.post("/webchat/sessions")).status_code == 200
        assert (await api.client.post("/webchat/sessions")).status_code == 200
        assert (await api.client.post("/webchat/sessions")).status_code == 429


# ----------------------------------------------------------------------------- consola
@pytest.fixture(scope="module")
def rsa_keys() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


async def test_admin_api_without_authentication_configured(api: Api) -> None:
    response = await api.client.get("/admin/api/me")
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


async def test_admin_api_with_an_oidc_token(rsa_keys: tuple[bytes, str]) -> None:
    private, public = rsa_keys
    verifier = JwtVerifier(OidcConfig(audience="iamia-admin", algorithms=["RS256"], public_key_pem=public))

    def configure(container: FakeContainer) -> None:
        container.verifier = verifier

    now = int(time.time())
    claims = {
        "sub": "ana",
        "preferred_username": "Ana",
        "aud": "iamia-admin",
        "iat": now,
        "exp": now + 300,
        "realm_access": {"roles": ["supervisor"]},
    }
    token = jwt.encode(claims, private, algorithm="RS256")
    async with running(_settings(), configure) as api:
        me = await api.client.get("/admin/api/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        body = me.json()
        assert body["subject"] == "ana" and body["name"] == "Ana" and body["roles"] == ["supervisor"]
        assert "conversations:reply" in body["permissions"] and "contacts:delete" not in body["permissions"]
        malformed = await api.client.get(
            "/admin/api/conversations/no-es-un-id", headers={"Authorization": f"Bearer {token}"}
        )
        assert malformed.status_code == 422  # identificador inválido: error del pedido, no del servidor
        expired = jwt.encode({**claims, "exp": now - 3600}, private, algorithm="RS256")
        assert (
            await api.client.get("/admin/api/me", headers={"Authorization": f"Bearer {expired}"})
        ).status_code == 401
