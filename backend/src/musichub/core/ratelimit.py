"""Einfaches Rate-Limit im Prozess (gleitendes Fenster je Schlüssel).

Ergänzt die Limits im nginx des Frontend-Containers für besonders empfindliche
Endpunkte (Anmeldung). Der Zustand liegt im Speicher des API-Prozesses – bei mehreren
API-Replikaten gilt das Limit je Replikat.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import Request

from musichub.core.errors import AppError


class TooManyRequestsError(AppError):
    status_code = 429
    code = "rate_limited"


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= self.max_keys and key not in self._hits:
                self._prune(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, int(self.window - (now - hits[0])) + 1)
                raise TooManyRequestsError(
                    "Zu viele Anfragen. Bitte kurz warten.", extra={"retry_after": retry}
                )
            hits.append(now)

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
            del self._hits[k]
        if len(self._hits) >= self.max_keys:
            self._hits.clear()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_key(request: Request) -> str:
    # Die Client-Adresse setzt uvicorn aus X-Forwarded-For (nginx im Frontend-Container
    # überschreibt den Header mit der echten Adresse, siehe frontend/nginx/default.conf).
    return request.client.host if request.client else "unknown"
