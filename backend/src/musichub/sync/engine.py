"""Synchronisations-Engine: berechnet, was auf einer Plattform-Playlist passieren muss.

Grundsatz: Die lokale Playlist ist die Wahrheit, die Richtung ist lokal -> Plattform.
Die Planung ist eine reine Funktion; ausgeführt wird der Plan von einem Job über die
ConnectorRegistry. So bleibt die Logik ohne Netzwerk testbar.

Tracks ohne bestätigte Verknüpfung zur Zielplattform werden übersprungen und im Plan
als ``unresolved`` ausgewiesen – sie bleiben lokal vollständig erhalten.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from musichub.domain.types import Platform


@dataclass(frozen=True, slots=True)
class DesiredItem:
    track_id: uuid.UUID
    #: Externe ID auf der Zielplattform; None = kein bestätigter Link.
    external_id: str | None


@dataclass(frozen=True, slots=True)
class SyncPlan:
    platform: Platform
    #: Vollständige Soll-Reihenfolge auf der Plattform.
    target_external_ids: tuple[str, ...]
    to_add: tuple[str, ...]
    to_remove: tuple[str, ...]
    unresolved_track_ids: tuple[uuid.UUID, ...]
    order_changed: bool

    @property
    def is_noop(self) -> bool:
        return not (self.to_add or self.to_remove or self.order_changed)


def plan_playlist_sync(
    platform: Platform,
    desired: Sequence[DesiredItem],
    remote_current: Sequence[str],
) -> SyncPlan:
    target = tuple(item.external_id for item in desired if item.external_id)
    unresolved = tuple(item.track_id for item in desired if not item.external_id)

    want, have = Counter(target), Counter(remote_current)
    to_add = tuple((want - have).elements())
    to_remove = tuple((have - want).elements())
    order_changed = not to_add and not to_remove and tuple(remote_current) != target

    return SyncPlan(
        platform=platform,
        target_external_ids=target,
        to_add=to_add,
        to_remove=to_remove,
        unresolved_track_ids=unresolved,
        order_changed=order_changed,
    )
