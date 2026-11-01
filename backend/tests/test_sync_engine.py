"""Reine Sync-Planung: Modi, externe Änderungen, Duplikate, fehlende Tracks, Idempotenz."""

from __future__ import annotations

import uuid

import pytest

from musichub.domain.types import Platform, SyncMode
from musichub.sync.engine import (
    DesiredItem,
    RemoteItem,
    SyncPlan,
    Unresolved,
    plan_sync,
    reorder_moves,
)

ADD, MIRROR = SyncMode.ADD_ONLY, SyncMode.MIRROR


def want(*ids: str | None) -> list[DesiredItem]:
    return [DesiredItem(uuid.uuid4(), i, None if i else Unresolved.NOT_FOUND) for i in ids]


def remote(*ids: str) -> list[RemoteItem]:
    return [RemoteItem(i, f"item-{n}-{i}") for n, i in enumerate(ids)]


def apply(plan: SyncPlan, current: list[RemoteItem]) -> list[RemoteItem]:
    """Simuliert die Ausführung (wie die Executor es tun)."""
    if plan.mode is MIRROR:
        return [RemoteItem(e, f"new-{n}-{e}") for n, e in enumerate(plan.target_sequence)]
    return current + [RemoteItem(a.external_id, f"new-{a.external_id}") for a in plan.to_add]


# --- add_only ------------------------------------------------------------------------------------


def test_add_only_appends_missing_and_keeps_external_tracks() -> None:
    plan = plan_sync(Platform.SPOTIFY, ADD, want("a", "b", "c"), remote("x", "a"), known=None)
    assert [a.external_id for a in plan.to_add] == ["b", "c"]
    assert plan.to_remove == () and not plan.reorder
    assert plan.target_sequence == ("x", "a", "b", "c")
    assert plan.already_present == 1
    assert plan.external_added == ("x", "a")  # erster Sync: alles Vorhandene ist extern
    assert not plan.requires_approval


def test_add_only_never_removes_or_reorders() -> None:
    plan = plan_sync(Platform.SPOTIFY, ADD, want("b", "a"), remote("a", "b", "z"), known=["a", "b"])
    assert plan.is_noop
    assert plan.external_added == ("z",)


def test_add_only_collapses_local_duplicates() -> None:
    plan = plan_sync(Platform.SPOTIFY, ADD, want("a", "b", "a"), remote(), known=None)
    assert [a.external_id for a in plan.to_add] == ["a", "b"]
    assert plan.duplicates_skipped == ("a",)


def test_add_only_does_not_duplicate_existing_remote_duplicates() -> None:
    plan = plan_sync(Platform.SPOTIFY, ADD, want("a"), remote("a", "a"), known=["a", "a"])
    assert plan.is_noop


def test_add_only_respects_external_removal() -> None:
    # MusicHub hatte a und b hinzugefügt; jemand hat b auf Spotify entfernt
    plan = plan_sync(Platform.SPOTIFY, ADD, want("a", "b", "c"), remote("a"), known=["a", "b"])
    assert [a.external_id for a in plan.to_add] == ["c"]
    assert plan.not_readded == ("b",)
    assert plan.external_removed == ("b",)


def test_unresolved_tracks_are_reported_not_dropped() -> None:
    items = want("a", None, "c")
    items[1] = DesiredItem(items[1].track_id, None, Unresolved.UNCHECKED)
    plan = plan_sync(Platform.YOUTUBE, ADD, items, remote(), known=None)
    assert [u.track_id for u in plan.unresolved] == [items[1].track_id]
    assert plan.unresolved[0].reason is Unresolved.UNCHECKED
    assert [a.external_id for a in plan.to_add] == ["a", "c"]


# --- mirror --------------------------------------------------------------------------------------


def test_mirror_reproduces_local_order_and_duplicates() -> None:
    plan = plan_sync(
        Platform.SPOTIFY, MIRROR, want("b", "a", "b"), remote("a", "b"), known=["a", "b"]
    )
    assert plan.target_sequence == ("b", "a", "b")
    assert [a.external_id for a in plan.to_add] == ["b"]
    assert plan.to_remove == ()
    assert plan.reorder
    assert not plan.requires_approval


def test_mirror_removes_own_tracks_without_approval() -> None:
    plan = plan_sync(Platform.SPOTIFY, MIRROR, want("a"), remote("a", "b"), known=["a", "b"])
    assert [(r.external_id, r.external) for r in plan.to_remove] == [("b", False)]
    assert not plan.requires_approval


def test_mirror_needs_approval_to_remove_external_tracks() -> None:
    plan = plan_sync(Platform.SPOTIFY, MIRROR, want("a"), remote("a", "x"), known=["a"])
    assert [(r.external_id, r.external) for r in plan.to_remove] == [("x", True)]
    assert plan.external_added == ("x",)
    assert plan.requires_approval


def test_mirror_needs_approval_to_readd_externally_removed() -> None:
    plan = plan_sync(Platform.SPOTIFY, MIRROR, want("a", "b"), remote("a"), known=["a", "b"])
    assert plan.readds_external_removed == ("b",)
    assert plan.requires_approval


def test_mirror_external_extra_copy_of_own_track() -> None:
    # MusicHub hat a einmal hinzugefügt, jemand hat a ein zweites Mal ergänzt
    plan = plan_sync(Platform.SPOTIFY, MIRROR, want("a"), remote("a", "a"), known=["a"])
    assert [(r.external_id, r.external) for r in plan.to_remove] == [("a", True)]
    assert plan.to_remove[0].item_id == "item-1-a"  # das hintere Vorkommen


def test_mirror_first_sync_treats_existing_content_as_external() -> None:
    plan = plan_sync(Platform.SPOTIFY, MIRROR, want("a"), remote("z"), known=None)
    assert plan.requires_approval and plan.to_remove[0].external


# --- Idempotenz ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [ADD, MIRROR])
@pytest.mark.parametrize(
    ("local", "start"),
    [
        (("a", "b", "c"), ()),
        (("a", "b", "a", "c"), ("c", "a")),
        (("c", "b", "a"), ("a", "b", "c")),
        ((), ("a",)),
    ],
)
def test_second_run_is_noop(mode: SyncMode, local: tuple[str, ...], start: tuple[str, ...]) -> None:
    current = remote(*start)
    known: list[str] | None = list(start) or None
    first = plan_sync(Platform.SPOTIFY, mode, want(*local), current, known)
    after = apply(first, current)
    second = plan_sync(Platform.SPOTIFY, mode, want(*local), after, [i.external_id for i in after])
    assert second.is_noop, second
    assert second.to_add == () and second.to_remove == ()


# --- Umsortieren ---------------------------------------------------------------------------------


def test_reorder_moves_reach_target() -> None:
    current = remote("a", "b", "c", "d", "b")
    target = ["b", "d", "a", "b", "c"]
    moves = reorder_moves(current, target)
    simulated = list(current)
    for move in moves:
        item = next(i for i in simulated if i.item_id == move.item_id)
        simulated.remove(item)
        simulated.insert(move.position, item)
    assert [i.external_id for i in simulated] == target
    assert reorder_moves(remote("a", "b"), ["a", "b"]) == []
    with pytest.raises(ValueError):
        reorder_moves(remote("a"), ["b"])


def test_add_only_memory_of_external_removal_survives() -> None:
    first = plan_sync(Platform.SPOTIFY, ADD, want("a", "b"), remote("a"), known=["a", "b"])
    assert first.not_readded == ("b",)
    # Der Executor speichert known = Schnittmenge + not_readded
    known = ["a", *first.not_readded]
    second = plan_sync(Platform.SPOTIFY, ADD, want("a", "b"), remote("a"), known=known)
    assert second.is_noop and second.not_readded == ("b",)
