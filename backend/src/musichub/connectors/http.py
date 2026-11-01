"""Gemeinsamer HTTP-Client der Connectoren.

* ``get`` liest (mit Wiederholungen), ``write`` (POST/PUT/DELETE) ist ausschließlich für
  die Synchronisierung bestimmt und wiederholt nie unkontrolliert. Lesende Abläufe (Import,
  Playlist-Liste, Metadaten) rufen nur ``get`` auf – ein Test prüft das.
* Ein Token wird nur an den eigenen API-Host gesendet (``next``-Links anderer Hosts werden
  abgelehnt).
* Rate-Limits (429): kurze ``Retry-After``-Werte werden direkt abgewartet, längere als
  ``RateLimitedError`` an den Job gegeben, der dann zeitversetzt neu startet.
* Fehlermeldungen enthalten nie Tokens oder Antwortkörper mit Zugangsdaten.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from musichub.connectors.base import (
    ConnectorAccessError,
    ConnectorAuthError,
    ConnectorError,
    RateLimitedError,
)
from musichub.domain.types import Platform

log = logging.getLogger(__name__)

#: Bis zu dieser Wartezeit (Sekunden) wartet der Client selbst, darüber übernimmt der Job.
INLINE_RETRY_AFTER = 5.0
MAX_ATTEMPTS = 3
USER_AGENT = "MusicHub/1.0 (+self-hosted; read-only)"

#: Wird von der Plattform bei 403 mit diesem Grund gemeldet: Kontingent erschöpft
QuotaCheck = Callable[[httpx.Response], float | None]


def _retry_after(response: httpx.Response, default: float = 30.0) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return default


def error_message(response: httpx.Response) -> str:
    """Kurze, ungefährliche Fehlermeldung aus der Antwort (ohne Rohdaten)."""
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = err.get("message")
        if isinstance(message, str) and message:
            return f"HTTP {response.status_code}: {message[:200]}"
    if isinstance(err, str):
        desc = data.get("error_description")
        return f"HTTP {response.status_code}: {err}" + (
            f" ({desc[:200]})" if isinstance(desc, str) else ""
        )
    return f"HTTP {response.status_code}"


class ApiClient:
    def __init__(
        self,
        platform: Platform,
        base_url: str,
        *,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
        auth_scheme: str = "Bearer",
        quota_check: QuotaCheck | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.platform = platform
        self.base_url = base_url.rstrip("/")
        self._host = urlsplit(self.base_url).netloc
        self._auth_scheme = auth_scheme
        self._quota_check = quota_check
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            if urlsplit(path_or_url).netloc != self._host:
                raise ConnectorError(
                    self.platform, "Verweis auf fremden Host abgelehnt.", retryable=False
                )
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"{self._auth_scheme} {token}"}

    def _check(self, response: httpx.Response) -> float | None:
        """Wirft passende Fehler für 4xx; gibt bei 429 die Wartezeit zurück, sonst None."""
        status = response.status_code
        if status == 429:
            return _retry_after(response)
        if status == 401:
            raise ConnectorAuthError(self.platform)
        if status == 403 and self._quota_check and (wait := self._quota_check(response)):
            raise RateLimitedError(
                self.platform,
                wait,
                "Kontingent der API erschöpft – der Vorgang wird nach dem Zurücksetzen "
                "fortgesetzt.",
            )
        if status in (400, 403, 404, 409):
            raise ConnectorAccessError(self.platform, error_message(response), status=status)
        return None

    def get(
        self, path_or_url: str, token: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        url = self._url(path_or_url)
        last_error = "unbekannter Fehler"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.get(url, params=params, headers=self._headers(token))
            except httpx.TimeoutException:
                last_error = "Zeitüberschreitung"
            except httpx.TransportError as exc:
                last_error = f"Verbindungsfehler ({type(exc).__name__})"
            else:
                if response.status_code < 300:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ConnectorError(self.platform, "Unerwartetes Antwortformat.")
                    return data
                wait = self._check(response)
                if wait is not None:
                    if wait <= INLINE_RETRY_AFTER and attempt < MAX_ATTEMPTS:
                        log.info("%s: Rate-Limit, warte %.0f s", self.platform.value, wait)
                        self._sleep(wait)
                        continue
                    raise RateLimitedError(self.platform, wait)
                last_error = error_message(response)  # 5xx: kurz warten und erneut versuchen
            if attempt < MAX_ATTEMPTS:
                self._sleep(attempt)
        raise ConnectorError(self.platform, f"Plattform nicht erreichbar: {last_error}")

    def write(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schreibender Aufruf – ausschließlich für die Synchronisierung (sync/executor.py).

        Keine automatische Wiederholung bei Zeitüberschreitung oder Serverfehler: Ob die
        Änderung angekommen ist, ist dann unbekannt. Der Fehler geht an den Sync, der den
        Lauf beendet; der nächste Lauf liest den tatsächlichen Stand und plant neu.
        Nur 429 (Anfrage wurde nicht ausgeführt) wird bei kurzer Wartezeit wiederholt."""
        if method not in {"POST", "PUT", "DELETE"}:
            raise ValueError(f"Unzulässige Methode {method}")
        url = self._url(path)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.request(
                    method, url, json=json, params=params, headers=self._headers(token)
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(
                    self.platform,
                    f"Schreibzugriff ohne Antwort ({type(exc).__name__}) – Ergebnis unbekannt, "
                    "der nächste Lauf prüft den tatsächlichen Stand.",
                    retryable=False,
                ) from None
            if response.status_code < 300:
                if not response.content:
                    return {}
                data = response.json()
                return data if isinstance(data, dict) else {}
            wait = self._check(response)
            if wait is not None:
                if wait <= INLINE_RETRY_AFTER and attempt < MAX_ATTEMPTS:
                    self._sleep(wait)
                    continue
                raise RateLimitedError(self.platform, wait)
            raise ConnectorError(
                self.platform,
                f"Schreibzugriff fehlgeschlagen: {error_message(response)}",
                retryable=False,
            )
        raise RateLimitedError(self.platform, INLINE_RETRY_AFTER)

    def close(self) -> None:
        self._client.close()


class OAuthClient:
    """POST an Token-/Widerrufs-Endpunkte (nicht an die Plattform-API)."""

    def __init__(
        self, platform: Platform, *, timeout: float, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.platform = platform
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )

    def post_form(
        self, url: str, data: Mapping[str, str], *, basic_auth: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.post(url, data=dict(data), auth=basic_auth)
        except httpx.HTTPError as exc:
            raise ConnectorError(
                self.platform, f"Anmeldedienst nicht erreichbar ({type(exc).__name__})."
            ) from None
        if response.status_code == 429:
            raise RateLimitedError(self.platform, _retry_after(response))
        if response.status_code in (400, 401, 403):
            # invalid_grant, invalid_client … – erneutes Verbinden nötig
            raise ConnectorAuthError(
                self.platform, f"Anmeldung abgelehnt ({error_message(response)})."
            )
        if response.status_code >= 300:
            raise ConnectorError(self.platform, f"Anmeldedienst: {error_message(response)}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def post_status(self, url: str, data: Mapping[str, str]) -> int:
        """Für Widerruf: nur der Statuscode zählt, Fehler werden geschluckt."""
        try:
            return self._client.post(url, data=dict(data)).status_code
        except httpx.HTTPError:
            return 0
