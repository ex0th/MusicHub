"""Zentrale Stelle für alle Connector-Aufrufe – sorgt für Fehlerisolation.

* Jede Ausnahme eines Connectors wird in ``ConnectorError`` übersetzt; nichts
  "Fremdes" (httpx-Fehler, KeyError aus kaputtem JSON …) erreicht den Aufrufer.
* Pro Plattform gibt es einen eigenen Circuit Breaker: Nach ``failure_threshold``
  wiederholbaren Fehlern in Folge wird die Plattform für ``cooldown_seconds``
  nicht mehr angesprochen. Andere Plattformen sind davon unberührt.
* Konfigurations- und "nicht implementiert"-Fehler zählen nicht als Störung.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from musichub.connectors.base import (
    Capability,
    ConnectorError,
    ConnectorUnavailableError,
    PlatformConnector,
)
from musichub.domain.types import Platform

log = logging.getLogger(__name__)

T = TypeVar("T")


class ConnectorState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    READY = "ready"
    DEGRADED = "degraded"  # letzte(r) Aufruf(e) fehlgeschlagen, Breaker noch zu
    SUSPENDED = "suspended"  # Breaker offen


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    platform: Platform
    state: ConnectorState
    capabilities: frozenset[Capability]
    consecutive_failures: int
    last_error: str | None
    suspended_for_seconds: float | None


@dataclass(slots=True)
class _Health:
    consecutive_failures: int = 0
    last_error: str | None = None
    open_until: float = 0.0


class ConnectorRegistry:
    def __init__(
        self,
        connectors: Iterable[PlatformConnector],
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connectors = {c.platform: c for c in connectors}
        self._health = {p: _Health() for p in self._connectors}
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def platforms(self) -> list[Platform]:
        return list(self._connectors)

    def get(self, platform: Platform) -> PlatformConnector:
        return self._connectors[platform]

    def call(self, platform: Platform, operation: Callable[[PlatformConnector], T]) -> T:
        """Führt ``operation`` isoliert für eine Plattform aus."""
        connector = self._connectors[platform]
        health = self._health[platform]

        with self._lock:
            remaining = health.open_until - self._clock()
        if remaining > 0:
            raise ConnectorUnavailableError(platform, remaining)

        try:
            result = operation(connector)
        except ConnectorError as exc:
            if exc.retryable:
                self._record_failure(platform, exc.message)
            raise
        except Exception as exc:  # Fremdfehler aus dem Connector einkapseln
            log.exception("Unerwarteter Fehler im %s-Connector", platform.value)
            message = f"{type(exc).__name__}: {exc}"
            self._record_failure(platform, message)
            raise ConnectorError(platform, message, retryable=True) from exc

        self._record_success(platform)
        return result

    def _record_failure(self, platform: Platform, message: str) -> None:
        with self._lock:
            health = self._health[platform]
            health.consecutive_failures += 1
            health.last_error = message
            if health.consecutive_failures >= self._threshold:
                health.open_until = self._clock() + self._cooldown
                log.warning(
                    "Connector %s für %.0f s ausgesetzt nach %d Fehlern: %s",
                    platform.value,
                    self._cooldown,
                    health.consecutive_failures,
                    message,
                )

    def _record_success(self, platform: Platform) -> None:
        with self._lock:
            health = self._health[platform]
            health.consecutive_failures = 0
            health.last_error = None
            health.open_until = 0.0

    def status(self) -> list[ConnectorStatus]:
        now = self._clock()
        result: list[ConnectorStatus] = []
        with self._lock:
            for platform, connector in self._connectors.items():
                health = self._health[platform]
                remaining = health.open_until - now
                if not connector.is_configured():
                    state = ConnectorState.NOT_CONFIGURED
                elif remaining > 0:
                    state = ConnectorState.SUSPENDED
                elif health.consecutive_failures > 0:
                    state = ConnectorState.DEGRADED
                else:
                    state = ConnectorState.READY
                result.append(
                    ConnectorStatus(
                        platform=platform,
                        state=state,
                        capabilities=connector.capabilities,
                        consecutive_failures=health.consecutive_failures,
                        last_error=health.last_error,
                        suspended_for_seconds=round(remaining, 1) if remaining > 0 else None,
                    )
                )
        return result
