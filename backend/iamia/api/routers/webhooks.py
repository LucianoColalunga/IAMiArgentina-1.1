"""Webhooks de las plataformas: GET para la suscripción, POST para los eventos.

El POST valida la firma sobre el cuerpo crudo, traduce al formato común, guarda y encola. Nunca
espera al modelo: las plataformas exigen respuesta rápida (Messenger 5 s, Meta mediana < 250 ms).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response

from iamia.api.deps import container
from iamia.channels.contracts import ChannelAdapter, WebhookRequest
from iamia.core.errors import ChannelDisabled, WebhookAuthError, WebhookPayloadError
from iamia.core.metrics import WEBHOOK_REJECTED

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _adapter(request: Request, channel: str) -> ChannelAdapter:
    adapter = container(request).registry.find(channel)
    if adapter is None or not adapter.accepts_webhooks:
        raise ChannelDisabled(channel)
    return adapter


def _public_url(request: Request) -> str:
    """URL como la vio la plataforma (detrás del proxy, con su esquema y host)."""
    settings = container(request).settings
    base = settings.public_base_url.rstrip("/")
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"{base}{path}" if base else str(request.url)


async def _webhook_request(request: Request) -> WebhookRequest:
    return WebhookRequest(
        method=request.method,
        url=_public_url(request),
        headers={key.lower(): value for key, value in request.headers.items()},
        query=dict(request.query_params),
        body=await request.body(),
        received_at=time.time(),
    )


@router.get("/{channel}")
async def subscribe(channel: str, request: Request) -> Response:
    adapter = _adapter(request, channel)
    assert adapter.verifier is not None
    reply = adapter.verifier.challenge(await _webhook_request(request))
    if reply is None:
        WEBHOOK_REJECTED.labels(channel, "challenge").inc()
        return Response("forbidden", status_code=403, media_type="text/plain")
    return Response(reply.body, status_code=reply.status_code, media_type=reply.media_type)


@router.post("/{channel}")
async def receive(channel: str, request: Request) -> Response:
    adapter = _adapter(request, channel)
    assert adapter.verifier is not None and adapter.parser is not None
    webhook = await _webhook_request(request)
    try:
        adapter.verifier.authenticate(webhook)
        batch = adapter.parser.parse(webhook)
    except WebhookAuthError:
        WEBHOOK_REJECTED.labels(channel, "signature").inc()
        raise
    except WebhookPayloadError:
        WEBHOOK_REJECTED.labels(channel, "payload").inc()
        raise
    trace_id = getattr(request.state, "request_id", "") or ""
    await container(request).ingest.ingest(batch, trace_id=trace_id)
    reply = adapter.webhook_reply
    if reply is not None:
        return Response(reply.body, status_code=reply.status_code, media_type=reply.media_type)
    return Response("ok", media_type="text/plain")
