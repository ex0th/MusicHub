"""Wiederverwendbare SQL-Bausteine rund um die Plattform-Verfügbarkeit von Tracks."""

from __future__ import annotations

from sqlalchemy import ColumnElement, ScalarSelect, and_, exists, func, select

from musichub.domain.types import Availability, LinkStatus, Platform
from musichub.models import Track, TrackSource


def source_is_available() -> ColumnElement[bool]:
    """Eine Quelle zählt, wenn sie bestätigt und nicht als unverfügbar markiert ist."""
    return and_(
        TrackSource.link_status == LinkStatus.LINKED.value,
        TrackSource.availability != Availability.UNAVAILABLE.value,
    )


def available_on(platform: Platform) -> ColumnElement[bool]:
    return exists(
        select(TrackSource.id).where(
            TrackSource.track_id == Track.id,
            TrackSource.platform == platform.value,
            source_is_available(),
        )
    )


def available_platform_count() -> ScalarSelect[int]:
    return (
        select(func.count(func.distinct(TrackSource.platform)))
        .where(TrackSource.track_id == Track.id, source_is_available())
        .scalar_subquery()
    )
