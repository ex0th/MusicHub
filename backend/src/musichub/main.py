"""FastAPI-Anwendung. Start: ``uvicorn musichub.main:app``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from musichub.api.routes import (
    connections,
    exports,
    health,
    imports,
    matching,
    playlists,
    sync,
    system,
    tags,
    tracks,
)
from musichub.auth.verifiers import build_verifier
from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import AppEnv, Settings, get_settings
from musichub.core.errors import register_exception_handlers
from musichub.core.logging import configure_logging
from musichub.core.middleware import RequestContextMiddleware

log = logging.getLogger("musichub")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "MusicHub API startet (env=%s, auth=%s, build=%s)",
            settings.app_env.value,
            settings.auth_mode.value,
            settings.build_sha,
        )
        yield
        log.info("MusicHub API beendet")

    # OpenAPI/Swagger ist read-only und enthält keine Geheimnisse; Aufrufe brauchen ein Token.
    app = FastAPI(
        title="MusicHub API",
        description="Selbst gehostete, plattformunabhängige Musikverwaltung.",
        version=health.APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.state.settings = settings
    app.state.verifier = build_verifier(settings)
    app.state.connectors = ConnectorRegistry(default_connectors(settings))

    register_exception_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(system.router)
    app.include_router(tracks.router)
    app.include_router(playlists.router)
    app.include_router(tags.router)
    app.include_router(imports.router)
    app.include_router(exports.router)
    app.include_router(matching.router)
    app.include_router(connections.router)
    app.include_router(connections.oauth_router)
    app.include_router(sync.router)

    if settings.app_env is AppEnv.PRODUCTION and settings.cors_origins == ["*"]:
        log.warning("CORS erlaubt alle Origins in production – bitte einschränken.")
    return app


def __getattr__(name: str) -> FastAPI:
    # ``app`` wird erst beim Zugriff gebaut, damit Tests/Tools das Modul ohne gültige
    # Umgebung importieren können (uvicorn greift auf musichub.main:app zu).
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
