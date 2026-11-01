"""Plattform-Konten: Anmeldung (OAuth), Token-Verwaltung, Trennen, Status.

Sicherheitsregeln:
* Tokens werden nur verschlüsselt gespeichert (``TokenCipher``) und nie zurückgegeben
  oder geloggt. ``OAuthTokens.__repr__`` blendet sie aus.
* ``state`` ist zufällig (256 Bit), wird nur als SHA-256 gespeichert, ist einmalig und
  zehn Minuten gültig. Der PKCE-Verifier liegt verschlüsselt in ``oauth_states``.
* Token-Erneuerung läuft in einer **eigenen, sofort festgeschriebenen Transaktion** mit
  Zeilensperre: Manche Dienste (SoundCloud) geben einmalige Refresh-Tokens aus – ginge
  das neue Token durch einen späteren Rollback verloren, wäre das Konto getrennt.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, literal_column, select
from sqlalchemy.orm import Session, sessionmaker

from musichub.auth import Principal
from musichub.connections.crypto import TokenCipher, TokenCipherError
from musichub.connectors.base import (
    Capability,
    ConnectorAuthError,
    ConnectorError,
    OAuthTokens,
    PlatformConnector,
)
from musichub.connectors.oauth import code_challenge, new_code_verifier, new_state
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings
from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.db.session import get_sessionmaker
from musichub.domain.types import AccountStatus, JobStatus, Platform, RemoteImportStatus
from musichub.jobs.queue import enqueue
from musichub.models import ConnectorAccount, Job, OAuthState, RemotePlaylist

log = logging.getLogger(__name__)

STATE_TTL = timedelta(minutes=10)
#: Token wird erneuert, wenn es in weniger als dieser Zeit abläuft
REFRESH_MARGIN = timedelta(minutes=2)

PLATFORM_LABEL = {
    Platform.SPOTIFY: "Spotify",
    Platform.YOUTUBE: "YouTube",
    Platform.SOUNDCLOUD: "SoundCloud",
}
_ENV = {
    Platform.SPOTIFY: ("MUSICHUB_SPOTIFY_CLIENT_ID", "MUSICHUB_SPOTIFY_CLIENT_SECRET"),
    Platform.YOUTUBE: ("MUSICHUB_YOUTUBE_CLIENT_ID", "MUSICHUB_YOUTUBE_CLIENT_SECRET"),
    Platform.SOUNDCLOUD: ("MUSICHUB_SOUNDCLOUD_CLIENT_ID", "MUSICHUB_SOUNDCLOUD_CLIENT_SECRET"),
}

#: Job-Typen dieser Etappe (alle lesend)
JOB_REFRESH_PLAYLISTS = "platform.refresh_playlists"
JOB_IMPORT_PLAYLIST = "platform.import_playlist"
JOB_REFRESH_METADATA = "platform.refresh_metadata"
JOB_KINDS = (JOB_REFRESH_PLAYLISTS, JOB_IMPORT_PLAYLIST, JOB_REFRESH_METADATA)
ACTIVE_JOB_STATES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.FAILED.value)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def redirect_uri(settings: Settings, platform: Platform) -> str:
    base = settings.oauth_redirect_base
    if not base:
        raise UnprocessableError("MUSICHUB_PUBLIC_URL ist nicht gesetzt.")
    return f"{base}/{platform.value}"


def config_problems(settings: Settings, connector: PlatformConnector) -> list[str]:
    """Was fehlt, damit ein Konto verbunden werden kann (Variablennamen, ohne Werte)."""
    problems: list[str] = []
    if not connector.is_configured():
        problems.extend(_ENV[connector.platform])
    if not settings.public_url:
        problems.append("MUSICHUB_PUBLIC_URL")
    try:
        TokenCipher.from_settings(settings)
    except TokenCipherError:
        problems.append("MUSICHUB_TOKEN_ENCRYPTION_KEY")
    return problems


def get_account(db: Session, platform: Platform) -> ConnectorAccount | None:
    return db.scalar(select(ConnectorAccount).where(ConnectorAccount.platform == platform.value))


def require_account(db: Session, platform: Platform) -> ConnectorAccount:
    account = get_account(db, platform)
    if account is None:
        raise ConflictError(f"Kein {PLATFORM_LABEL[platform]}-Konto verbunden.")
    return account


# --- Anmeldung ----------------------------------------------------------------------------


def start_authorization(
    db: Session,
    settings: Settings,
    registry: ConnectorRegistry,
    platform: Platform,
    principal: Principal,
    *,
    write: bool = False,
) -> str:
    """Legt einen einmaligen ``state`` an und liefert die Anmelde-URL der Plattform.

    ``write`` fordert zusätzlich Schreibrechte für Playlists an – nur erlaubt, wenn die
    Synchronisierung für diese Plattform freigeschaltet ist (MUSICHUB_SYNC_WRITE_PLATFORMS)."""
    connector = registry.get(platform)
    if write and not (connector.write_scopes and settings.sync_write_enabled(platform.value)):
        raise UnprocessableError(
            f"Schreibzugriff für {PLATFORM_LABEL[platform]} ist nicht freigeschaltet "
            "(MUSICHUB_SYNC_WRITE_PLATFORMS).",
        )
    if Capability.OAUTH not in connector.capabilities:
        raise UnprocessableError(f"{PLATFORM_LABEL[platform]} unterstützt keine Anmeldung.")
    if problems := config_problems(settings, connector):
        raise UnprocessableError(
            f"{PLATFORM_LABEL[platform]} ist nicht eingerichtet. Es fehlt: {', '.join(problems)}.",
            extra={"missing": problems},
        )
    cipher = TokenCipher.from_settings(settings)
    db.execute(delete(OAuthState).where(OAuthState.expires_at < _now()))
    state, verifier = new_state(), new_code_verifier()
    now = _now()
    db.add(
        OAuthState(
            state_hash=_hash(state),
            platform=platform.value,
            code_verifier_enc=cipher.encrypt(verifier),
            access="write" if write else "read",
            created_by=principal.subject,
            created_at=now,
            expires_at=now + STATE_TTL,
        )
    )
    db.flush()
    uri = redirect_uri(settings, platform)
    return registry.call(
        platform,
        lambda c: c.authorization_url(
            state=state, code_challenge=code_challenge(verifier), redirect_uri=uri, write=write
        ),
    )


class AuthorizationError(Exception):
    """Fehler im Rückruf der Plattform. ``code`` landet als Hinweis im Frontend-Link."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def complete_authorization(
    db: Session,
    settings: Settings,
    registry: ConnectorRegistry,
    platform: Platform,
    *,
    state: str | None,
    code: str | None,
    error: str | None,
) -> ConnectorAccount:
    """Rückruf der Plattform: Code gegen Tokens tauschen, Konto speichern.

    Der ``state`` wird in jedem Fall verbraucht (auch bei Fehlern)."""
    if not state:
        raise AuthorizationError("invalid_state", "Anmeldung ohne state-Parameter.")
    pending = db.scalar(
        select(OAuthState)
        .where(OAuthState.state_hash == _hash(state), OAuthState.platform == platform.value)
        .with_for_update()
    )
    if pending is None:
        raise AuthorizationError("invalid_state", "Unbekannte oder bereits verwendete Anmeldung.")
    db.delete(pending)
    db.flush()
    if pending.expires_at < _now():
        raise AuthorizationError("expired", "Anmeldung abgelaufen. Bitte erneut verbinden.")
    if error:
        raise AuthorizationError("denied", "Anmeldung bei der Plattform abgebrochen.")
    if not code:
        raise AuthorizationError("invalid_request", "Kein Autorisierungscode erhalten.")

    cipher = TokenCipher.from_settings(settings)
    verifier = cipher.decrypt(pending.code_verifier_enc)
    uri = redirect_uri(settings, platform)
    try:
        tokens = registry.call(
            platform,
            lambda c: c.exchange_code(code=code, code_verifier=verifier, redirect_uri=uri),
        )
        remote = registry.call(platform, lambda c: c.get_account(tokens.access_token))
    except ConnectorError as exc:
        log.warning("Anmeldung bei %s fehlgeschlagen: %s", platform.value, exc.message)
        raise AuthorizationError("exchange_failed", exc.message) from None

    account = get_account(db, platform)
    now = _now()
    if account is None:
        account = ConnectorAccount(platform=platform.value, connected_at=now)
        db.add(account)
    elif account.external_user_id != remote.external_id:
        # Anderes Konto: Playlist-Liste des alten Kontos gilt nicht mehr (Importe bleiben)
        db.execute(
            delete(RemotePlaylist).where(
                RemotePlaylist.platform == platform.value,
                RemotePlaylist.local_playlist_id.is_(None),
            )
        )
        account.connected_at = now
        account.playlists_synced_at = None
    account.external_user_id = remote.external_id
    account.display_name = remote.display_name[:255]
    account.connected_by = pending.created_by
    _store_tokens(account, tokens, cipher)
    if tokens.scope is None:
        # Dienst meldet keine Scopes: angeforderte übernehmen
        connector = registry.get(platform)
        requested = connector.write_scopes if pending.access == "write" else connector.scopes
        account.scopes = " ".join(requested) or None
    db.flush()
    enqueue_unique(db, JOB_REFRESH_PLAYLISTS, {"platform": platform.value})
    log.info("Konto verbunden", extra={"platform": platform.value})
    return account


def _store_tokens(account: ConnectorAccount, tokens: OAuthTokens, cipher: TokenCipher) -> None:
    account.access_token_enc = cipher.encrypt(tokens.access_token)
    if tokens.refresh_token:
        account.refresh_token_enc = cipher.encrypt(tokens.refresh_token)
    account.token_expires_at = tokens.expires_at
    if tokens.scope is not None:
        account.scopes = tokens.scope
    account.status = AccountStatus.CONNECTED.value
    account.last_error = None


# --- Tokens im Hintergrund -----------------------------------------------------------------


def _mark_reauth(sessions: sessionmaker[Session], platform: Platform, message: str) -> None:
    with sessions.begin() as s:
        account = get_account(s, platform)
        if account is not None:
            account.status = AccountStatus.REAUTH_REQUIRED.value
            account.last_error = message[:1000]


def access_token(
    settings: Settings,
    registry: ConnectorRegistry,
    platform: Platform,
    *,
    force_refresh: bool = False,
    sessions: sessionmaker[Session] | None = None,
) -> str:
    """Gültiges Access-Token; erneuert es bei Bedarf (eigene Transaktion, Zeilensperre)."""
    sessions = sessions or get_sessionmaker()
    cipher = TokenCipher.from_settings(settings)
    failure: ConnectorAuthError | None = None
    with sessions.begin() as s:
        account = s.scalar(
            select(ConnectorAccount)
            .where(ConnectorAccount.platform == platform.value)
            .with_for_update()
        )
        if account is None:
            raise ConnectorAuthError(platform, "Kein Konto verbunden.")
        if account.status == AccountStatus.REAUTH_REQUIRED.value:
            raise ConnectorAuthError(platform, "Konto muss neu verbunden werden.")
        expires = account.token_expires_at
        fresh = expires is None or expires - _now() > REFRESH_MARGIN
        if fresh and not force_refresh:
            return cipher.decrypt(account.access_token_enc)
        refresh = cipher.decrypt_optional(account.refresh_token_enc)
        if not refresh:
            failure = ConnectorAuthError(platform, "Anmeldung abgelaufen. Bitte neu verbinden.")
        else:
            try:
                tokens = registry.call(platform, lambda c: c.refresh_tokens(refresh))
            except ConnectorAuthError as exc:
                failure = exc
            else:
                _store_tokens(account, tokens, cipher)
                return tokens.access_token
        # Status festschreiben (Block endet regulär), danach den Fehler weitergeben
        account.status = AccountStatus.REAUTH_REQUIRED.value
        account.last_error = failure.message[:1000]
    raise failure


def call_with_token[T](
    settings: Settings,
    registry: ConnectorRegistry,
    platform: Platform,
    operation: Callable[[PlatformConnector, str], T],
    *,
    sessions: sessionmaker[Session] | None = None,
) -> T:
    """Plattformaufruf mit gültigem Token; bei 401 einmal erneuern und wiederholen."""
    token = access_token(settings, registry, platform, sessions=sessions)
    try:
        return registry.call(platform, lambda c: operation(c, token))
    except ConnectorAuthError:
        token = access_token(settings, registry, platform, force_refresh=True, sessions=sessions)
        try:
            return registry.call(platform, lambda c: operation(c, token))
        except ConnectorAuthError as exc:
            _mark_reauth(sessions or get_sessionmaker(), platform, exc.message)
            raise


# --- Trennen ---------------------------------------------------------------------------------


def disconnect(
    db: Session, settings: Settings, registry: ConnectorRegistry, platform: Platform
) -> bool:
    """Widerruft (falls möglich) und löscht die Tokens. Importierte Daten bleiben erhalten.

    Gibt zurück, ob die Plattform den Widerruf bestätigt hat."""
    account = db.scalar(
        select(ConnectorAccount)
        .where(ConnectorAccount.platform == platform.value)
        .with_for_update()
    )
    if account is None:
        raise NotFoundError(f"Kein {PLATFORM_LABEL[platform]}-Konto verbunden.")
    revoked = False
    try:
        cipher = TokenCipher.from_settings(settings)
        tokens = OAuthTokens(
            access_token=cipher.decrypt(account.access_token_enc),
            refresh_token=cipher.decrypt_optional(account.refresh_token_enc),
            expires_at=account.token_expires_at,
        )
        revoked = registry.call(platform, lambda c: c.revoke(tokens))
    except (TokenCipherError, ConnectorError) as exc:
        log.info("Widerruf bei %s nicht möglich: %s", platform.value, exc)
    db.delete(account)
    # Noch nie importierte Playlists vergessen; importierte behalten ihre Zuordnung
    db.execute(
        delete(RemotePlaylist).where(
            RemotePlaylist.platform == platform.value, RemotePlaylist.local_playlist_id.is_(None)
        )
    )
    db.execute(delete(OAuthState).where(OAuthState.platform == platform.value))
    db.flush()
    log.info("Konto getrennt", extra={"platform": platform.value, "revoked": revoked})
    return revoked


# --- Jobs ------------------------------------------------------------------------------------


def enqueue_unique(db: Session, kind: str, payload: dict[str, Any], **kw: Any) -> Job:
    """Plant einen Job ein, sofern kein gleicher wartet oder läuft."""
    stmt = select(Job).where(Job.kind == kind, Job.status.in_(ACTIVE_JOB_STATES))
    for key, value in payload.items():
        if key in {"platform", "remote_playlist_id"}:
            stmt = stmt.where(Job.payload[key].astext == str(value))
    existing = db.scalar(stmt.limit(1))
    if existing is not None:
        return existing
    return enqueue(db, kind, payload, **kw)


def platform_jobs(db: Session, platform: Platform | None, *, limit: int) -> list[Job]:
    stmt = select(Job).where(Job.kind.in_(JOB_KINDS))
    if platform:
        stmt = stmt.where(Job.payload["platform"].astext == platform.value)
    return list(db.scalars(stmt.order_by(Job.created_at.desc()).limit(limit)))


# --- Status ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    platform: Platform
    configured: bool
    missing: list[str]
    connector_state: str
    capabilities: list[str]
    account: ConnectorAccount | None
    playlists: int
    imported: int
    changed: int
    failed: int
    active_jobs: int


def statuses(
    db: Session, settings: Settings, registry: ConnectorRegistry
) -> list[ConnectionStatus]:
    accounts = {a.platform: a for a in db.scalars(select(ConnectorAccount))}
    counts: dict[str, dict[str, int]] = {}
    for platform, imported, changed, failed, total in db.execute(
        select(
            RemotePlaylist.platform,
            func.count().filter(RemotePlaylist.local_playlist_id.is_not(None)),
            func.count().filter(
                RemotePlaylist.local_playlist_id.is_not(None),
                RemotePlaylist.imported_version.is_distinct_from(RemotePlaylist.remote_version),
            ),
            func.count().filter(
                RemotePlaylist.last_import_status == RemoteImportStatus.ERROR.value
            ),
            func.count().filter(RemotePlaylist.removed_at.is_(None)),
        ).group_by(RemotePlaylist.platform)
    ):
        counts[platform] = {
            "imported": imported,
            "changed": changed,
            "failed": failed,
            "playlists": total,
        }
    platform_col = literal_column("jobs.payload->>'platform'")
    active = dict(
        db.execute(
            select(platform_col, func.count())
            .where(Job.kind.in_(JOB_KINDS), Job.status.in_(ACTIVE_JOB_STATES))
            .group_by(platform_col)
        ).all()
    )
    connector_states = {s.platform: s for s in registry.status()}
    result = []
    for platform in registry.platforms:
        connector = registry.get(platform)
        missing = config_problems(settings, connector)
        c = counts.get(platform.value, {})
        state = connector_states[platform]
        result.append(
            ConnectionStatus(
                platform=platform,
                configured=not missing,
                missing=missing,
                connector_state=state.state.value,
                capabilities=sorted(cap.value for cap in connector.capabilities),
                account=accounts.get(platform.value),
                playlists=c.get("playlists", 0),
                imported=c.get("imported", 0),
                changed=c.get("changed", 0),
                failed=c.get("failed", 0),
                active_jobs=int(active.get(platform.value, 0)),
            )
        )
    return result
