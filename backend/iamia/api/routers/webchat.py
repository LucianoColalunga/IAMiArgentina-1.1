"""Chat web propio: sesión anónima firmada, envío de mensajes y respuestas por long polling.

El navegador no habla con el modelo: manda su mensaje, que sigue el mismo camino que cualquier
otro canal (cola, worker, dispatcher), y espera la respuesta con long polling. La sesión es un
identificador aleatorio firmado con HMAC; no se usan cookies.
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from iamia.ai.limits import WindowLimiter
from iamia.api.deps import client_ip, container
from iamia.channels.models import Channel, InboundMessage, MessageKind, WebhookBatch
from iamia.core.clock import utcnow
from iamia.core.errors import ChannelDisabled, InvalidRequest, QuotaExceeded, Unauthorized
from iamia.security.session_tokens import SessionTokens

router = APIRouter(prefix="/webchat", tags=["webchat"])


class SessionResponse(BaseModel):
    token: str
    expires_at: int


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=1000)
    client_message_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9-]*$")


class SendResponse(BaseModel):
    accepted: bool
    duplicated: bool = False


def _tokens(request: Request) -> SessionTokens:
    tokens: SessionTokens | None = request.app.state.session_tokens
    if tokens is None or Channel.WEBCHAT not in container(request).registry.channels:
        raise ChannelDisabled("webchat")
    return tokens


def _session(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("falta la sesión")
    return _tokens(request).verify(token.strip())  # Unauthorized si es inválido o venció


@router.post("/sessions", response_model=SessionResponse)
async def create_session(request: Request) -> SessionResponse:
    tokens = _tokens(request)
    app = container(request)
    limiter = WindowLimiter(app.redis, "webchat_sessions", app.settings.webchat_sessions_per_ip_per_hour, 3600)
    allowed, retry_after = await limiter.hit(client_ip(request))
    if not allowed:
        raise QuotaExceeded("Se crearon demasiadas sesiones desde tu conexión. Probá más tarde.", retry_after)
    token, expires = tokens.issue(SessionTokens.new_session_id())
    return SessionResponse(token=token, expires_at=expires)


@router.post("/messages", response_model=SendResponse, status_code=202)
async def send_message(body: SendRequest, request: Request) -> SendResponse:
    session_id = _session(request)
    text = body.text.strip()
    if not text:
        raise InvalidRequest("El mensaje está vacío.")
    message = InboundMessage(
        channel=Channel.WEBCHAT,
        provider_message_id=f"{session_id}:{body.client_message_id or uuid.uuid4().hex}",
        sender_id=session_id,
        account_id="webchat",
        kind=MessageKind.TEXT,
        sent_at=utcnow(),
        text=text,
    )
    trace_id = getattr(request.state, "request_id", "") or ""
    result = await container(request).ingest.ingest(WebhookBatch(messages=(message,)), trace_id=trace_id)
    return SendResponse(accepted=result.accepted == 1, duplicated=result.duplicated == 1)


@router.get("/messages")
async def poll_messages(request: Request, after: int = 0, wait: bool = True) -> dict[str, object]:
    session_id = _session(request)
    app = container(request)
    deadline = time.monotonic() + (app.settings.webchat_long_poll_seconds if wait else 0)
    while True:
        messages = await app.webchat_repo.messages_after(session_id, max(after, 0))
        remaining = deadline - time.monotonic()
        if messages or remaining <= 0:
            return {"messages": messages}
        await request.app.state.webchat_hub.wait(session_id, max_wait=min(remaining, 25.0))
