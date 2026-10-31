from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from musichub.api.schemas import ConnectorStatusOut, LibraryStats, MeResponse
from musichub.auth import Principal, current_principal
from musichub.connectors.registry import ConnectorRegistry
from musichub.db.session import get_db
from musichub.library.stats import library_stats

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/me", response_model=MeResponse, summary="Aktuell angemeldeter Benutzer")
def me(principal: Principal = Depends(current_principal)) -> MeResponse:
    return MeResponse(
        subject=principal.subject,
        display_name=principal.display_name,
        groups=sorted(principal.groups),
        can_write=principal.can_write,
        auth_method=principal.auth_method,
    )


@router.get(
    "/connectors",
    response_model=list[ConnectorStatusOut],
    summary="Status aller Plattform-Connectoren (dieses API-Prozesses)",
)
def connectors(
    request: Request, _: Principal = Depends(current_principal)
) -> list[ConnectorStatusOut]:
    registry: ConnectorRegistry = request.app.state.connectors
    return [
        ConnectorStatusOut(
            platform=s.platform,
            state=s.state,
            capabilities=sorted(s.capabilities),
            consecutive_failures=s.consecutive_failures,
            last_error=s.last_error,
            suspended_for_seconds=s.suspended_for_seconds,
        )
        for s in registry.status()
    ]


@router.get("/stats", response_model=LibraryStats, summary="Kennzahlen der Bibliothek")
def stats(db: Session = Depends(get_db), _: Principal = Depends(current_principal)) -> LibraryStats:
    return library_stats(db)
