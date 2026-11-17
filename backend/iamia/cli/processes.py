"""Procesos de larga duración: api, worker, dispatcher y maintenance."""

from __future__ import annotations

import logging
from collections.abc import Awaitable

from iamia.bootstrap import Container
from iamia.channels.models import Channel
from iamia.core.logs import configure_logging
from iamia.core.tracing import setup_tracing
from iamia.settings import csv_list, get_settings
from iamia.workers.runtime import HealthServer, run_until_stopped

logger = logging.getLogger(__name__)


def run_api(host: str, port: int) -> int:
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level, role="api")
    uvicorn.run(
        "iamia.api.app:build",
        factory=True,
        host=host,
        port=port,
        workers=max(1, settings.web_concurrency),
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips="*" if settings.trust_proxy_headers else None,
        access_log=False,  # las métricas HTTP y los logs propios no registran query strings
        log_config=None,
        server_header=False,
        timeout_graceful_shutdown=20,
        timeout_keep_alive=30,
    )
    return 0


def _selected_channels(container: Container, requested: str) -> list[Channel]:
    available = list(container.registry.channels)
    if not requested:
        return available
    wanted = {name.lower() for name in csv_list(requested)}
    unknown = wanted - {channel.value for channel in available}
    if unknown:
        raise SystemExit(f"canales no habilitados en esta instancia: {', '.join(sorted(unknown))}")
    return [channel for channel in available if channel.value in wanted]


def _tasks(role: str, container: Container, channels: str) -> list[Awaitable[None]]:
    settings = container.settings
    if role == "worker":
        processor = container.inbound_processor()
        return [container.queue.consume("inbound", processor.handle, prefetch=settings.inbound_prefetch)]
    if role == "dispatcher":
        dispatcher = container.outbound_dispatcher()
        selected = _selected_channels(container, channels)
        logger.info("dispatcher_channels", extra={"ctx": {"channels": [c.value for c in selected]}})
        return [
            container.queue.consume(f"outbound.{channel.value}", dispatcher.handle, prefetch=settings.outbound_prefetch)
            for channel in selected
        ]
    return [container.maintenance_runner().run()]


async def run_role(role: str, *, channels: str = "") -> int:
    settings = get_settings()
    configure_logging(settings.log_level, role=role)
    setup_tracing(role, settings.otel_exporter_otlp_endpoint, sample_ratio=settings.otel_traces_sample_ratio)
    container = await Container.create(settings, role=role)
    health = HealthServer(
        settings.metrics_port,
        readiness=[container.ping_database, container.ping_redis, container.queue.ping],
    )
    await health.start()
    logger.info("role_started", extra={"ctx": {"role": role}})
    try:
        await run_until_stopped(_tasks(role, container, channels))
    finally:
        await health.stop()
        await container.close()
    return 0
