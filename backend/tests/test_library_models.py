"""Datenmodell: Constraints und Kaskaden direkt in der Datenbank prüfen."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from musichub.domain.types import Availability, LinkStatus
from musichub.models import Playlist, PlaylistItem, Tag, Track, TrackSource, track_tags

pytestmark = pytest.mark.db


def make_track(db: Session, title: str = "Song", **kw: object) -> Track:
    track = Track(title=title, artist="Artist", **kw)
    db.add(track)
    db.flush()
    return track


def test_track_without_any_source_is_valid(db: Session) -> None:
    track = make_track(db, version="Extended Mix")
    db.commit()
    assert track.sources == []
    assert track.version == "Extended Mix"


@pytest.mark.parametrize(
    "fields", [{"duration_ms": 0}, {"isrc": "kein-isrc"}, {"isrc": "usum71900001"}]
)
def test_track_check_constraints(db: Session, fields: dict) -> None:
    db.add(Track(title="x", artist="y", **fields))
    with pytest.raises(IntegrityError):
        db.flush()


def test_only_one_linked_source_per_platform(db: Session) -> None:
    track = make_track(db)
    db.add(TrackSource(track_id=track.id, platform="spotify", external_id="a" * 22))
    db.flush()
    # weiterer Kandidat ist erlaubt …
    db.add(
        TrackSource(
            track_id=track.id,
            platform="spotify",
            external_id="b" * 22,
            link_status=LinkStatus.CANDIDATE.value,
        )
    )
    db.flush()
    # … eine zweite bestätigte Quelle nicht
    db.add(TrackSource(track_id=track.id, platform="spotify", external_id="c" * 22))
    with pytest.raises(IntegrityError):
        db.flush()


def test_external_track_linked_to_one_local_track_only(db: Session) -> None:
    a, b = make_track(db, "A"), make_track(db, "B")
    db.add(TrackSource(track_id=a.id, platform="youtube", external_id="dQw4w9WgXcQ"))
    db.flush()
    db.add(TrackSource(track_id=b.id, platform="youtube", external_id="dQw4w9WgXcQ"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_source_defaults_and_platform_check(db: Session) -> None:
    track = make_track(db)
    source = TrackSource(track_id=track.id, platform="spotify", external_id="x" * 22)
    db.add(source)
    db.flush()
    db.refresh(source)
    assert source.availability == Availability.UNKNOWN
    assert source.link_status == LinkStatus.LINKED
    db.add(TrackSource(track_id=track.id, platform="tidal", external_id="1"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_deleting_track_cascades_sources_and_tags(db: Session) -> None:
    tag = Tag(name="House", category="Genre")
    track = make_track(db)
    track.tags.append(tag)
    db.add(TrackSource(track_id=track.id, platform="spotify", external_id="x" * 22))
    db.commit()

    db.delete(track)
    db.commit()
    assert db.scalar(select(func.count()).select_from(TrackSource)) == 0
    assert db.scalar(select(func.count()).select_from(track_tags)) == 0
    assert db.get(Tag, tag.id) is not None  # Tag selbst bleibt


def test_tag_names_unique_per_category_case_insensitive(db: Session) -> None:
    db.add(Tag(name="House", category="Genre"))
    db.add(Tag(name="House", category="Stimmung"))  # andere Kategorie: erlaubt
    db.flush()
    db.add(Tag(name="house", category="genre"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_track_in_playlist_cannot_be_deleted_directly(db: Session) -> None:
    track = make_track(db)
    playlist = Playlist(name="P")
    db.add(playlist)
    db.flush()
    db.add(PlaylistItem(playlist_id=playlist.id, track_id=track.id, position=0))
    db.commit()
    db.delete(track)
    with pytest.raises(IntegrityError):
        db.commit()


def test_playlist_positions_unique_checked_at_commit(db: Session) -> None:
    track = make_track(db)
    playlist = Playlist(name="P")
    db.add(playlist)
    db.flush()
    db.add_all(
        [
            PlaylistItem(playlist_id=playlist.id, track_id=track.id, position=0),
            PlaylistItem(playlist_id=playlist.id, track_id=track.id, position=0),
        ]
    )
    db.flush()  # DEFERRABLE: Zwischenzustand ist erlaubt …
    with pytest.raises(IntegrityError):
        db.commit()  # … beim Commit nicht mehr
