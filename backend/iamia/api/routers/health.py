"""Salud y métricas.

- /healthz (y /health/live por compatibilidad): el proceso responde.
- /readyz (y /health/ready): PostgreSQL, Redis y la cola responden. El modelo no es requisito
  de readiness de la API: si el LLM cae, la API sigue recibiendo y encolando mensajes.
- /metrics: formato Prometheus (el proxy no lo expone a internet).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from iamia import __version__
from iamia.api.deps import container
from iamia.core.metrics import render_metrics

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    app = container(request)
    names = ("database", "redis", "queue")
    results = await asyncio.gather(app.ping_database(), app.ping_redis(), app.queue.ping(), return_exceptions=True)
    checks = {name: result is True for name, result in zip(names, results, strict=True)}
    ok = all(checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status_code=200 if ok else 503,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/metrics")
async def metrics() -> Response:
    data, content_type = render_metrics()
    return Response(content=data, media_type=content_type)
