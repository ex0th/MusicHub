import uuid

from musichub.domain.types import Platform
from musichub.sync.engine import DesiredItem, plan_playlist_sync


def item(external_id: str | None) -> DesiredItem:
    return DesiredItem(track_id=uuid.uuid4(), external_id=external_id)


def test_noop_when_identical() -> None:
    plan = plan_playlist_sync(Platform.SPOTIFY, [item("a"), item("b")], ["a", "b"])
    assert plan.is_noop


def test_adds_removes_and_reports_unresolved() -> None:
    missing = item(None)
    plan = plan_playlist_sync(Platform.SPOTIFY, [item("a"), missing, item("c")], ["a", "b"])
    assert plan.target_external_ids == ("a", "c")
    assert plan.to_add == ("c",)
    assert plan.to_remove == ("b",)
    assert plan.unresolved_track_ids == (missing.track_id,)
    assert not plan.is_noop


def test_reorder_detected() -> None:
    plan = plan_playlist_sync(Platform.YOUTUBE, [item("b"), item("a")], ["a", "b"])
    assert plan.order_changed
    assert not plan.to_add and not plan.to_remove


def test_duplicates_are_counted() -> None:
    plan = plan_playlist_sync(Platform.SOUNDCLOUD, [item("a"), item("a")], ["a"])
    assert plan.to_add == ("a",)
