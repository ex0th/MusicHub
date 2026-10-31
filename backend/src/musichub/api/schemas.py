"""Pydantic-Schemas der REST-API. Getrennt von den ORM-Modellen, damit die API stabil bleibt."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from musichub.connectors.base import Capability
from musichub.connectors.registry import ConnectorState
from musichub.domain.types import (
    Availability,
    ImportDecision,
    ImportItemStatus,
    ImportResult,
    ImportStatus,
    LinkStatus,
    MatchMethod,
    Platform,
    PlaylistImportMode,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
#: ISRC wird vor der Prüfung bereinigt ("us-um7-19-00001" -> "USUM71900001").
Isrc = Annotated[
    str,
    BeforeValidator(
        lambda v: v.replace("-", "").replace(" ", "").upper() if isinstance(v, str) else v
    ),
    StringConstraints(pattern=r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$"),
]


def _blank_to_none(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _optional_text(max_length: int) -> object:
    return Annotated[
        Annotated[str, StringConstraints(max_length=max_length)] | None,
        BeforeValidator(_blank_to_none),
    ]


Album = _optional_text(500)
Version = _optional_text(200)
Notes = _optional_text(10_000)
Url = _optional_text(1000)
ExternalId = _optional_text(255)
OptionalIsrc = Annotated[Isrc | None, BeforeValidator(_blank_to_none)]
Duration = Annotated[int | None, Field(gt=0, le=24 * 3600 * 1000)]

#: Obergrenze für IDs in einer Anfrage (Tags, Tracks zum Hinzufügen …)
MAX_BATCH = 500


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InputModel(BaseModel):
    """Basis für Request-Bodies: unbekannte Felder sind ein Fehler (Tippfehler fallen auf)."""

    model_config = ConfigDict(extra="forbid")


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


# --- Tags ----------------------------------------------------------------------

TagName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
TagCategory = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class TagCreate(InputModel):
    name: TagName
    category: TagCategory = "Allgemein"


class TagUpdate(InputModel):
    name: TagName | None = None
    category: TagCategory | None = None


class TagOut(ApiModel):
    id: uuid.UUID
    name: str
    category: str


class TagWithCountOut(TagOut):
    track_count: int


# --- Track-Quellen -------------------------------------------------------------


class SourceCreate(InputModel):
    """Entweder ``url`` (Plattform und ID werden erkannt) oder ``platform`` + ``external_id``."""

    platform: Platform | None = None
    external_id: ExternalId = None
    url: Url = None
    availability: Availability = Availability.UNKNOWN
    link_status: LinkStatus = LinkStatus.LINKED

    @model_validator(mode="after")
    def _need_identifier(self) -> SourceCreate:
        if not self.url and not (self.platform and self.external_id):
            raise ValueError("Entweder url oder platform und external_id angeben.")
        return self


class SourceUpdate(InputModel):
    external_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = (
        None
    )
    url: Url = None
    availability: Availability | None = None
    link_status: LinkStatus | None = None
    #: Ohne Angabe wird der Zeitpunkt beim Ändern von ``availability`` automatisch gesetzt.
    last_checked_at: datetime | None = None


class SourceOut(ApiModel):
    id: uuid.UUID
    track_id: uuid.UUID
    platform: Platform
    external_id: str
    url: str | None
    link_status: LinkStatus
    availability: Availability
    match_method: MatchMethod
    match_score: float | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- Tracks --------------------------------------------------------------------


class TrackCreate(InputModel):
    title: NonEmpty
    artist: NonEmpty
    album: Album = None
    version: Version = None
    duration_ms: Duration = None
    isrc: OptionalIsrc = None
    release_date: date | None = None
    notes: Notes = None
    tag_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_BATCH)] = []
    sources: Annotated[list[SourceCreate], Field(max_length=10)] = []


class TrackUpdate(InputModel):
    """Nur übergebene Felder werden geändert. ``null`` leert ein optionales Feld."""

    title: NonEmpty | None = None
    artist: NonEmpty | None = None
    album: Album = None
    version: Version = None
    duration_ms: Duration = None
    isrc: OptionalIsrc = None
    release_date: date | None = None
    notes: Notes = None
    #: Ersetzt alle Tags des Tracks.
    tag_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_BATCH)] | None = None

    @model_validator(mode="after")
    def _required_not_null(self) -> TrackUpdate:
        for name in ("title", "artist"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} darf nicht leer sein.")
        if "tag_ids" in self.model_fields_set and self.tag_ids is None:
            raise ValueError("tag_ids muss eine Liste sein (leer = alle Tags entfernen).")
        return self


class TrackOut(ApiModel):
    id: uuid.UUID
    title: str
    artist: str
    album: str | None
    version: str | None
    duration_ms: int | None
    isrc: str | None
    release_date: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    sources: list[SourceOut]
    tags: list[TagOut]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def platforms(self) -> list[Platform]:
        """Plattformen mit bestätigter, nicht als unverfügbar markierter Quelle."""
        return sorted(
            {
                s.platform
                for s in self.sources
                if s.link_status is LinkStatus.LINKED
                and s.availability is not Availability.UNAVAILABLE
            }
        )


class PlaylistRef(ApiModel):
    id: uuid.UUID
    name: str
    occurrences: int


class TrackDetailOut(TrackOut):
    playlists: list[PlaylistRef]


class TrackSort(StrEnum):
    TITLE = "title"
    ARTIST = "artist"
    ALBUM = "album"
    VERSION = "version"
    DURATION = "duration"
    RELEASE_DATE = "release_date"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class Page[T](ApiModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class TagAssignment(InputModel):
    tag_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_BATCH)]


# --- Playlists -----------------------------------------------------------------

PlaylistName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]


class PlaylistCreate(InputModel):
    name: PlaylistName
    description: Notes = None
    track_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_BATCH)] = []


class PlaylistUpdate(InputModel):
    name: PlaylistName | None = None
    description: Notes = None

    @model_validator(mode="after")
    def _name_not_null(self) -> PlaylistUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name darf nicht leer sein.")
        return self


class PlaylistSummaryOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str | None
    item_count: int
    total_duration_ms: int
    created_at: datetime
    updated_at: datetime


class PlaylistItemOut(ApiModel):
    id: uuid.UUID
    position: int
    added_at: datetime
    track: TrackOut


class PlaylistDetailOut(PlaylistSummaryOut):
    items: list[PlaylistItemOut]


class PlaylistItemsAdd(InputModel):
    track_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=MAX_BATCH)]
    #: Einfügeposition (0 = an den Anfang). Ohne Angabe: ans Ende.
    position: Annotated[int, Field(ge=0)] | None = None
    #: Tracks, die schon in der Playlist sind, überspringen.
    skip_existing: bool = False


class PlaylistItemMove(InputModel):
    position: Annotated[int, Field(ge=0)]


class PlaylistReorder(InputModel):
    """Vollständige neue Reihenfolge: jede Eintrags-ID der Playlist genau einmal."""

    item_ids: Annotated[list[uuid.UUID], Field(max_length=10_000)]


# --- Statistik -----------------------------------------------------------------


class PlatformStats(ApiModel):
    platform: Platform
    #: Tracks mit verfügbarer, bestätigter Quelle auf dieser Plattform
    tracks: int
    #: davon ausschließlich auf dieser Plattform verfügbar
    exclusive: int
    #: bestätigte Quellen, die als nicht verfügbar markiert sind
    unavailable: int


class LibraryStats(ApiModel):
    tracks: int
    playlists: int
    tags: int
    #: Tracks ohne verfügbare Plattform-Quelle
    local_only: int
    #: Tracks, die auf genau einer Plattform verfügbar sind
    single_platform: int
    platforms: list[PlatformStats]


# --- Import ----------------------------------------------------------------------


class ImportFormatOut(ApiModel):
    id: str
    label: str
    kind: str


class ImportFormatsOut(ApiModel):
    formats: list[ImportFormatOut]
    max_bytes: int
    max_rows: int
    extensions: list[str]


class ImportPlaylistPlan(ApiModel):
    name: str
    description: str | None
    track_count: int
    existing_playlist_id: uuid.UUID | None
    mode: PlaylistImportMode


class ImportUnassigned(ApiModel):
    count: int
    enabled: bool
    name: str


class ImportRunOut(ApiModel):
    id: uuid.UUID
    filename: str
    size_bytes: int
    format: str | None
    format_label: str | None
    status: ImportStatus
    created_by: str
    created_at: datetime
    committed_at: datetime | None
    warnings: list[str]
    error: str | None
    #: Zähler je Status; nach dem Commit zusätzlich created/linked/skipped/failed/playlists
    summary: dict[str, Any]
    playlists: list[ImportPlaylistPlan]
    unassigned: ImportUnassigned | None


class ImportSourceOut(ApiModel):
    platform: Platform
    external_id: str
    url: str


class ImportTrackRef(ApiModel):
    id: uuid.UUID
    title: str
    artist: str
    version: str | None


class ImportItemOut(ApiModel):
    id: uuid.UUID
    row: int
    status: ImportItemStatus
    decision: ImportDecision
    title: str | None
    artist: str | None
    version: str | None
    album: str | None
    duration_ms: int | None
    isrc: str | None
    sources: list[ImportSourceOut]
    playlists: list[str]
    errors: list[str]
    warnings: list[str]
    match: ImportTrackRef | None
    match_reason: str | None
    match_score: float | None
    duplicate_of_row: int | None
    result: ImportResult | None
    result_track_id: uuid.UUID | None
    result_message: str | None


class ImportDecisionUpdate(InputModel):
    decision: ImportDecision
    #: anderer vorhandener Track als der vorgeschlagene (nur bei use_existing)
    match_track_id: uuid.UUID | None = None


class ImportPlaylistMode(InputModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    mode: PlaylistImportMode


class ImportUnassignedUpdate(InputModel):
    enabled: bool | None = None
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
        | None
    ) = None


class ImportOptionsUpdate(InputModel):
    playlists: Annotated[list[ImportPlaylistMode], Field(max_length=1000)] = []
    unassigned: ImportUnassignedUpdate | None = None


# --- Connectoren ---------------------------------------------------------------


class ConnectorStatusOut(ApiModel):
    platform: Platform
    state: ConnectorState
    capabilities: list[Capability]
    consecutive_failures: int
    last_error: str | None
    suspended_for_seconds: float | None
