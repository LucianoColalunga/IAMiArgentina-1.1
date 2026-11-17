"""POST /v1/chat: consulta directa y sincrónica (compatible con la API de la versión 1).

Sirve para integraciones simples y pruebas. No guarda conversación: responde con el motor y
devuelve el mismo esquema que la versión 1.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from iamia.ai.engine import Outcome
from iamia.ai.limits import WindowLimiter
from iamia.api.deps import client_ip, container, verify_edge
from iamia.core.errors import QuotaExceeded

router = APIRouter(tags=["consultas"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(..., min_length=1, max_length=4000, description="Consulta del ciudadano")


class Source(BaseModel):
    title: str
    url: str


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source] = Field(default_factory=list)
    cached: bool = False
    cache_layer: Literal["redis", "semantic", "coalesced"] | None = None
    blocked: bool = False
    block_reason: str | None = None
    kb_version: str | None = None
    usage: Usage = Field(default_factory=Usage)
    request_id: str
    latency_ms: int


@router.post("/v1/chat", response_model=ChatResponse, dependencies=[Depends(verify_edge)])
async def chat(body: ChatRequest, request: Request, response: Response) -> ChatResponse:
    app = container(request)
    ip = client_ip(request)
    limiter = WindowLimiter(app.redis, "chat", app.settings.rate_limit_per_minute, 60)
    allowed, retry_after = await limiter.hit(ip)
    if not allowed:
        raise QuotaExceeded("Hiciste demasiadas consultas seguidas. Esperá un momento e intentá de nuevo.", retry_after)
    answer = await app.engine.answer(body.question, client_key=f"ip:{ip}")
    request_id = getattr(request.state, "request_id", "") or ""
    blocked = answer.outcome in (Outcome.BLOCKED, Outcome.REFUSED) or bool(answer.block_reason)
    response.headers["X-Cache"] = (
        f"HIT-{answer.cache_layer.upper()}" if answer.cache_layer else ("BLOCKED" if blocked else "MISS")
    )
    response.headers["Cache-Control"] = "private, no-store"
    return ChatResponse(
        answer=answer.text,
        sources=[Source(title=s.title, url=s.url) for s in answer.sources],
        cached=answer.cached,
        cache_layer=answer.cache_layer,  # type: ignore[arg-type]
        blocked=blocked,
        block_reason=answer.block_reason,
        kb_version=answer.kb_version,
        usage=Usage(input_tokens=answer.input_tokens, output_tokens=answer.output_tokens),
        request_id=request_id,
        latency_ms=answer.latency_ms,
    )
