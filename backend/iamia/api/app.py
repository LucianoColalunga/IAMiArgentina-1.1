"""Aplicación FastAPI (rol "api" / gateway)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from iamia import __version__
from iamia.api.errors import install_error_handlers
from iamia.api.middleware import RequestContextMiddleware
from iamia.api.routers import admin, ask, health, webchat, webhooks
from iamia.bootstrap import Container
from iamia.core.logs import configure_logging
from iamia.core.tracing import setup_tracing
from iamia.integrations.webchat.adapter import WebchatHub
from iamia.settings import Settings, get_settings

logger = logging.getLogger(__name__)

ContainerFactory = Callable[[Settings], Awaitable[Container]]
FRONTEND_DIR = Path(os.getenv("IAMIA_FRONTEND_DIR") or Path(__file__).resolve().parents[3] / "frontend")
MAX_BODY_BYTES = 3 * 1024 * 1024  # los webhooks de WhatsApp pueden pesar hasta 3 MB


async def _default_factory(settings: Settings) -> Container:
    return await Container.create(settings, role="api")


def create_app(settings: Settings | None = None, *, factory: ContainerFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    build = factory or _default_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await build(settings)
        hub = WebchatHub(container.redis)
        await hub.start()
        app.state.container = container
        app.state.webchat_hub = hub
        app.state.token_verifier = container.token_verifier() if settings.admin_api_enabled else None
        app.state.session_tokens = container.session_tokens()
        logger.info("api_started", extra={"ctx": {"channels": [c.value for c in container.registry.channels]}})
        try:
            yield
        finally:
            await hub.stop()
            await container.close()

    app = FastAPI(
        title="IAMiArgentina",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(webchat.router)
    app.include_router(ask.router)
    if settings.admin_api_enabled:
        app.include_router(admin.router)

    if settings.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware, max_body_bytes=MAX_BODY_BYTES, hsts=settings.is_production)

    webchat_dir = FRONTEND_DIR / "webchat"
    console_dir = FRONTEND_DIR / "console"
    if settings.webchat_ui_enabled and webchat_dir.is_dir():
        app.mount("/chat", StaticFiles(directory=webchat_dir, html=True), name="chat")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/chat/")

    if settings.console_ui_enabled and settings.admin_api_enabled and console_dir.is_dir():
        app.mount("/console", StaticFiles(directory=console_dir, html=True), name="console")
    return app


def build() -> FastAPI:
    """Fábrica para uvicorn: cada proceso configura sus propios logs antes de armar la app."""
    settings = get_settings()
    configure_logging(settings.log_level, role="api")
    setup_tracing("api", settings.otel_exporter_otlp_endpoint, sample_ratio=settings.otel_traces_sample_ratio)
    return create_app(settings)
