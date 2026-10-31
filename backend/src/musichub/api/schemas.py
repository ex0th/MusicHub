"""Pydantic-Schemas der REST-API. Getrennt von den ORM-Modellen, damit die API stabil bleibt."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from musichub.connectors.base import Capability
from musichub.connectors.registry import ConnectorState
from musichub.domain.types import LinkStatus, MatchMethod, Platform

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
#: ISRC wird vor der Prüfung bereinigt ("us-um7-19-00001" -> "USUM71900001").
Isrc = Annotated[
    str,
    BeforeValidator(
        lambda v: v.replace("-", "").replace(" ", "").upper() if isinstance(v, str) else v
    ),
    StringConstraints(pattern=r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$"),
]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Health --------------------------------------------------------------------


class LiveResponse(ApiModel):
    status: Literal["ok"] = "ok"
    version: str
    build: str


class CheckResult(ApiModel):
    status: Literal["ok", "error"]
    detail: str | None = None


class ReadyResponse(ApiModel):
    status: Literal["ok", "error"]
    checks: dict[str, CheckResult]


# --- Auth ----------------------------------------------------------------------


class MeResponse(ApiModel):
    subject: str
    display_name: str
    groups: list[str]
    can_write: bool
    auth_method: str


# --- Tracks --------------------------------------------------------------------


class PlatformLinkOut(ApiModel):
    id: uuid.UUID
    platform: Platform
    external_id: str
    url: str | None
    status: LinkStatus
    match_method: MatchMethod
    match_score: float | None


class TrackBase(ApiModel):
    title: NonEmpty
    artist: NonEmpty
    album: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    duration_ms: Annotated[int, Field(gt=0)] | None = None
    isrc: Isrc | None = None
    notes: str | None = None


class TrackCreate(TrackBase):
    pass


class TrackUpdate(ApiModel):
    title: NonEmpty | None = None
    artist: NonEmpty | None = None
    album: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    duration_ms: Annotated[int, Field(gt=0)] | None = None
    isrc: Isrc | None = None
    notes: str | None = None


class TrackOut(TrackBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    links: list[PlatformLinkOut] = []


class Page[T](ApiModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# --- Connectoren ---------------------------------------------------------------


class ConnectorStatusOut(ApiModel):
    platform: Platform
    state: ConnectorState
    capabilities: list[Capability]
    consecutive_failures: int
    last_error: str | None
    suspended_for_seconds: float | None
