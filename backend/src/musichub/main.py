"""FastAPI-Anwendung. Start: ``uvicorn musichub.main:app``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from musichub.api.routes import (
    auth,
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
from musichub.auth.sessions import SessionManager
from musichub.auth.verifiers import build_provider, build_verifier
from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings, get_settings
from musichub.core.errors import register_exception_handlers
from musichub.core.logging import configure_logging
from musichub.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from musichub.core.ratelimit import RateLimiter

log = logging.getLogger("musichub")


def create_app(
    settings: Settings | None = None, *, oidc_transport: httpx.BaseTransport | None = None
) -> FastAPI:
    """App bauen. ``oidc_transport`` ersetzt in Tests das Netzwerk zum Identity-Provider."""
    settings = settings or get_settings()
    provider = build_provider(settings, oidc_transport)
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
        if provider is not None:
            provider.close()
        log.info("MusicHub API beendet")

    # OpenAPI/Swagger enthält keine Geheimnisse; in production standardmäßig aus.
    docs = settings.api_docs_enabled
    app = FastAPI(
        title="MusicHub API",
        description="Selbst gehostete, plattformunabhängige Musikverwaltung.",
        version=health.APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )

    app.state.settings = settings
    app.state.oidc = provider
    app.state.verifier = build_verifier(settings, provider)
    app.state.sessions = SessionManager(settings) if settings.oidc_login_enabled else None
    app.state.auth_limiter = RateLimiter(settings.auth_rate_limit_per_minute)
    app.state.connectors = ConnectorRegistry(default_connectors(settings))

    register_exception_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            # Sitzungs-Cookies nie an andere Origins: fremde Clients nutzen Bearer-Tokens
            allow_credentials=False,
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
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

    if settings.auth_mode.value == "oidc" and not settings.oidc_login_enabled:
        log.warning(
            "OIDC ohne Browser-Anmeldung: MUSICHUB_OIDC_CLIENT_ID, MUSICHUB_OIDC_CLIENT_SECRET "
            "und MUSICHUB_PUBLIC_URL setzen. Bis dahin sind nur Bearer-Tokens möglich."
        )
    return app


def __getattr__(name: str) -> FastAPI:
    # ``app`` wird erst beim Zugriff gebaut, damit Tests/Tools das Modul ohne gültige
    # Umgebung importieren können (uvicorn greift auf musichub.main:app zu).
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
