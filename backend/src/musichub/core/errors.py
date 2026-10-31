"""Fehlerbehandlung: einheitliche Antworten im Format RFC 9457 (application/problem+json).

Interne Details (Stacktraces, SQL) verlassen niemals die API; sie landen im Log,
verknüpft über die Request-ID, die auch in der Antwort steht.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from musichub.core.logging import correlation_id

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    """Basis für fachliche Fehler, die bewusst an den Client gehen."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, detail: str, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"


def problem(
    status: int, code: str, detail: str, *, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://musichub.local/problems/{code}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
        "detail": detail,
        "request_id": correlation_id.get(),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return problem(exc.status_code, exc.code, exc.detail, headers=headers, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        return problem(exc.status_code, code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return problem(422, "validation_error", "Die Anfrage ist ungültig.", errors=errors)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unbehandelter Fehler bei %s %s", request.method, request.url.path)
        return problem(500, "internal_error", "Interner Fehler. Details stehen im Server-Log.")
