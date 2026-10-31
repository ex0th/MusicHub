"""Hilfen für OAuth 2.0: PKCE und Auswertung von Token-Antworten."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from musichub.connectors.base import ConnectorAuthError, OAuthTokens
from musichub.domain.types import Platform


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    """RFC 7636: 43–128 Zeichen aus dem URL-sicheren Alphabet."""
    return secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def tokens_from_payload(
    platform: Platform, payload: dict[str, Any], *, previous_refresh: str | None = None
) -> OAuthTokens:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ConnectorAuthError(platform, "Antwort des Anmeldedienstes enthält kein Token.")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, int | float | str) and str(expires_in).isdigit()
        else None
    )
    scope = payload.get("scope")
    return OAuthTokens(
        access_token=access,
        # Manche Dienste liefern beim Erneuern kein neues Refresh-Token: altes weiterverwenden
        refresh_token=refresh if isinstance(refresh, str) and refresh else previous_refresh,
        expires_at=expires_at,
        scope=scope if isinstance(scope, str) else None,
    )
