"""Öffentliche Health-Checks (ohne Authentifizierung, ohne interne Details).

* ``/api/health/live``  – Prozess läuft. Für Docker-HEALTHCHECK/Liveness.
* ``/api/health/ready`` – Datenbank erreichbar und Migrationen aktuell. 503 sonst.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from musichub.api.schemas import CheckResult, LiveResponse, ReadyResponse
from musichub.db.migrations_state import code_heads, database_revisions
from musichub.db.session import get_engine

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"])

try:
    APP_VERSION = version("musichub-backend")
except PackageNotFoundError:  # pragma: no cover - nur ohne Installation
    APP_VERSION = "0.0.0"


@router.get("/live", response_model=LiveResponse)
def live(request: Request) -> LiveResponse:
    return LiveResponse(version=APP_VERSION, build=request.app.state.settings.build_sha)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "Nicht bereit"}},
)
def ready() -> JSONResponse:
    checks: dict[str, CheckResult] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = CheckResult(status="ok")
            current = database_revisions(conn)
        expected = code_heads()
        if current == expected:
            checks["migrations"] = CheckResult(status="ok")
        else:
            checks["migrations"] = CheckResult(
                status="error",
                detail="Datenbank-Schema ist nicht aktuell (alembic upgrade head ausführen).",
            )
    except SQLAlchemyError as exc:
        log.warning("Readiness: Datenbank nicht erreichbar: %s", exc.__class__.__name__)
        checks["database"] = CheckResult(status="error", detail="Datenbank nicht erreichbar.")

    ok = all(c.status == "ok" for c in checks.values())
    body = ReadyResponse(status="ok" if ok else "error", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)
