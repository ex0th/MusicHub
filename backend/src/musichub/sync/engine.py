"""Synchronisations-Engine: berechnet, was auf einer Zielplaylist passieren soll.

Reine Funktionen ohne I/O. Richtung: **MusicHub → Plattform**. Die lokale Playlist ist die
Wahrheit; lokale Daten werden hier nie verändert.

Zwei Modi:

``add_only`` (Standard)
    Fehlende Tracks werden angehängt. Nichts wird entfernt oder umsortiert. Tracks, die
    MusicHub früher hinzugefügt hat und die jemand auf der Plattform wieder entfernt hat,
    werden **nicht erneut** hinzugefügt (externe Entscheidung wird respektiert).
    Doppelte lokale Einträge werden nur einmal übertragen.

``mirror`` („vollständig abgleichen“)
    Die Zielplaylist wird exakt auf die lokale Reihenfolge gebracht (inkl. Entfernen und
    Umsortieren). Nur für von MusicHub verwaltete, ausdrücklich freigegebene Ziele.
    Entfernen von Tracks, die nicht von MusicHub stammen, oder erneutes Hinzufügen extern
    entfernter Tracks verlangt eine ausdrückliche Zustimmung (``requires_approval``).

Externe Änderungen werden über ``known`` erkannt: die Liste externer IDs, die nach dem
letzten erfolgreichen Sync auf der Zielplaylist stand. ``None`` = noch nie synchronisiert;
dann gilt alles Vorhandene als extern.

Idempotenz: Wird ein Plan ausgeführt und danach mit dem neuen Stand erneut geplant, ist
das Ergebnis leer (``is_noop``).
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from musichub.domain.types import Platform, SyncMode


class Unresolved(StrEnum):
    """Warum ein lokaler Track nicht übertragen werden kann."""

    #: noch nicht auf der Plattform gesucht
    UNCHECKED = "unchecked"
    #: gesucht, nicht gefunden
    NOT_FOUND = "not_found"
    #: Suche fehlgeschlagen (API-Fehler/Kontingent) – wird erneut versucht
    API_ERROR = "api_error"
    #: nur ähnlicher Treffer gefunden – wartet auf Bestätigung
    CANDIDATE = "candidate"
    #: verknüpft, aber auf der Plattform als nicht verfügbar markiert
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DesiredItem:
    track_id: uuid.UUID
    #: Externe ID auf der Zielplattform; None = nicht übertragbar (siehe ``reason``)
    external_id: str | None
    reason: Unresolved | None = None


@dataclass(frozen=True, slots=True)
class RemoteItem:
    external_id: str
    #: ID des Eintrags in der Playlist (YouTube: playlistItem-ID; Spotify: keine)
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedAdd:
    external_id: str
    track_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PlannedRemove:
    external_id: str
    item_id: str | None
    #: nicht von MusicHub hinzugefügt (bzw. mehr Vorkommen als von MusicHub stammen)
    external: bool


@dataclass(frozen=True, slots=True)
class SyncPlan:
    platform: Platform
    mode: SyncMode
    #: Sollzustand der Zielplaylist nach dem Sync
    target_sequence: tuple[str, ...]
    to_add: tuple[PlannedAdd, ...]
    to_remove: tuple[PlannedRemove, ...]
    reorder: bool
    unresolved: tuple[DesiredItem, ...]
    #: lokal doppelt vorhandene Tracks, die nur einmal übertragen werden (add_only)
    duplicates_skipped: tuple[str, ...] = ()
    #: seit dem letzten Sync auf der Plattform hinzugekommen (nicht von MusicHub)
    external_added: tuple[str, ...] = ()
    #: von MusicHub hinzugefügt und auf der Plattform entfernt
    external_removed: tuple[str, ...] = ()
    #: add_only: wegen externer Entfernung nicht erneut hinzugefügt
    not_readded: tuple[str, ...] = ()
    #: im Ziel schon vorhanden (add_only) – nichts zu tun
    already_present: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_noop(self) -> bool:
        return not (self.to_add or self.to_remove or self.reorder)

    @property
    def external_removals(self) -> tuple[PlannedRemove, ...]:
        return tuple(r for r in self.to_remove if r.external)

    @property
    def readds_external_removed(self) -> tuple[str, ...]:
        removed = set(self.external_removed)
        return tuple(a.external_id for a in self.to_add if a.external_id in removed)

    @property
    def requires_approval(self) -> bool:
        """Mirror: externe Tracks entfernen oder extern Entferntes wieder hinzufügen."""
        return self.mode is SyncMode.MIRROR and bool(
            self.external_removals or self.readds_external_removed
        )


def _external_changes(
    remote_ids: Sequence[str], known: Sequence[str] | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if known is None:
        return tuple(dict.fromkeys(remote_ids)), ()
    have, had = Counter(remote_ids), Counter(known)
    added = tuple((have - had).elements())
    removed = tuple((had - have).elements())
    return added, removed


def plan_sync(
    platform: Platform,
    mode: SyncMode,
    desired: Sequence[DesiredItem],
    remote: Sequence[RemoteItem],
    known: Sequence[str] | None,
) -> SyncPlan:
    remote_ids = [r.external_id for r in remote]
    unresolved = tuple(d for d in desired if not d.external_id)
    resolved = [d for d in desired if d.external_id]
    external_added, external_removed = _external_changes(remote_ids, known)

    if mode is SyncMode.ADD_ONLY:
        present = set(remote_ids)
        blocked = set(external_removed)
        planned: set[str] = set()
        adds: list[PlannedAdd] = []
        duplicates: list[str] = []
        not_readded: list[str] = []
        already = 0
        for d in resolved:
            ext = d.external_id
            assert ext is not None
            if ext in planned:
                duplicates.append(ext)
            elif ext in present:
                already += 1
                planned.add(ext)
            elif ext in blocked:
                not_readded.append(ext)
                planned.add(ext)
            else:
                adds.append(PlannedAdd(ext, d.track_id))
                planned.add(ext)
        return SyncPlan(
            platform=platform,
            mode=mode,
            target_sequence=tuple(remote_ids) + tuple(a.external_id for a in adds),
            to_add=tuple(adds),
            to_remove=(),
            reorder=False,
            unresolved=unresolved,
            duplicates_skipped=tuple(duplicates),
            external_added=external_added,
            external_removed=external_removed,
            not_readded=tuple(not_readded),
            already_present=already,
        )

    # --- mirror: exakte Kopie der lokalen Reihenfolge --------------------------------------
    target = [d.external_id for d in resolved if d.external_id]
    want, have = Counter(target), Counter(remote_ids)
    # Hinzufügen: Vorkommen über den vorhandenen hinaus, in Zielreihenfolge
    budget = Counter(have)
    adds = []
    for d in resolved:
        ext = d.external_id
        assert ext is not None
        if budget[ext] > 0:
            budget[ext] -= 1
        else:
            adds.append(PlannedAdd(ext, d.track_id))
    # Entfernen: überzählige Vorkommen, jeweils die hintersten
    surplus = have - want
    known_count = Counter(known or [])
    # Von MusicHub stammende Vorkommen, die nach dem Behalten übrig bleiben: nur die
    # dürfen ohne Zustimmung entfernt werden (behaltene Vorkommen zählen zuerst als eigene)
    musichub_owned = Counter(
        {k: max(0, min(v, have[k]) - min(want[k], have[k])) for k, v in known_count.items()}
    )
    removes: list[PlannedRemove] = []
    for item in reversed(remote):
        ext = item.external_id
        if surplus[ext] > 0:
            surplus[ext] -= 1
            # Vorkommen, die über die von MusicHub stammenden hinausgehen, sind extern
            owned = musichub_owned[ext] > 0
            if owned:
                musichub_owned[ext] -= 1
            removes.append(PlannedRemove(ext, item.item_id, external=not owned))
    removes.reverse()
    removed_ids = Counter(r.external_id for r in removes)
    remaining: list[str] = []
    for ext in remote_ids:
        if removed_ids[ext] > 0:
            removed_ids[ext] -= 1
        else:
            remaining.append(ext)
    after = remaining + [a.external_id for a in adds]
    return SyncPlan(
        platform=platform,
        mode=mode,
        target_sequence=tuple(target),
        to_add=tuple(adds),
        to_remove=tuple(removes),
        reorder=after != target,
        unresolved=unresolved,
        external_added=external_added,
        external_removed=external_removed,
    )


@dataclass(frozen=True, slots=True)
class Move:
    item_id: str
    position: int


def reorder_moves(current: Sequence[RemoteItem], target: Sequence[str]) -> list[Move]:
    """Einzelne Verschiebungen, die ``current`` in die Reihenfolge ``target`` bringen.

    Für Plattformen ohne „alles ersetzen“ (YouTube: ``playlistItems.update`` mit Position).
    Erwartet, dass ``current`` dieselben Elemente wie ``target`` enthält."""
    items = list(current)
    if Counter(i.external_id for i in items) != Counter(target):
        raise ValueError("Ist- und Sollzustand enthalten unterschiedliche Tracks.")
    moves: list[Move] = []
    for position, ext in enumerate(target):
        if items[position].external_id == ext:
            continue
        source = next(i for i in range(position + 1, len(items)) if items[i].external_id == ext)
        item = items.pop(source)
        items.insert(position, item)
        if item.item_id is None:
            raise ValueError("Verschieben braucht Eintrags-IDs.")
        moves.append(Move(item.item_id, position))
    return moves
