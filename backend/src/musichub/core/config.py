"""Zentrale Konfiguration. Alle Werte kommen aus Umgebungsvariablen (siehe .env.example)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    #: Statisches Entwicklungs-Token. Nur außerhalb von production erlaubt.
    DEV = "dev"
    #: JWT-Validierung gegen einen OIDC-Provider (z. B. Authentik).
    OIDC = "oidc"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSICHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        # "MUSICHUB_X=" in .env bedeutet "nicht gesetzt" statt leerer String
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: AppEnv = AppEnv.DEVELOPMENT
    #: Wird beim Image-Build gesetzt (Git-Commit), sonst "dev".
    build_sha: str = "dev"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.TEXT

    database_url: str = Field(
        default="postgresql+psycopg://musichub:musichub@localhost:5432/musichub",
        description="SQLAlchemy-URL, Treiber psycopg 3",
    )

    #: Erlaubte Browser-Origins (nur relevant, wenn Frontend und API getrennt laufen).
    #: Sitzungs-Cookies werden nie an fremde Origins freigegeben (keine Credentials).
    cors_origins: list[str] = Field(default_factory=list)
    #: Swagger-UI und OpenAPI-Schema unter /api/docs. Standard: aus in production.
    api_docs: bool | None = None

    # --- Authentifizierung ---------------------------------------------------
    auth_mode: AuthMode = AuthMode.DEV
    dev_auth_token: SecretStr | None = None
    dev_auth_user: str = "dev"

    #: Issuer des Authentik-Providers, z. B. https://auth.example.com/application/o/musichub/
    oidc_issuer: str | None = None
    #: Client-ID und -Secret der Authentik-Anwendung (Client-Typ „confidential“)
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    #: Erwartetes "aud" von Bearer-Access-Tokens (Standard: Client-ID)
    oidc_audience: str | None = None
    oidc_scopes: str = "openid profile email"
    #: Optional; sonst aus {issuer}/.well-known/openid-configuration ermittelt.
    oidc_jwks_url: str | None = None
    #: Gruppe/Rolle, die Schreibrechte bekommt. Leer = jeder angemeldete Nutzer.
    oidc_write_group: str | None = None
    oidc_groups_claim: str = "groups"
    #: Nur Mitglieder dieser Gruppe dürfen sich überhaupt anmelden (leer = Authentik
    #: entscheidet allein über die Policy-Bindung der Anwendung)
    oidc_required_group: str | None = None
    #: Beim Abmelden auch zur Abmeldung bei Authentik weiterleiten (end_session_endpoint)
    oidc_logout_at_provider: bool = True

    # --- Sitzungen -------------------------------------------------------------
    #: Sitzung endet nach so vielen Minuten ohne Aktivität
    session_idle_minutes: int = Field(default=720, ge=5, le=43_200)
    #: absolute Höchstdauer einer Sitzung in Stunden (danach neue Anmeldung)
    session_max_hours: int = Field(default=168, ge=1, le=2_160)
    #: Secure-Flag der Cookies. Standard: an, wenn MUSICHUB_PUBLIC_URL mit https beginnt.
    session_cookie_secure: bool | None = None
    #: Anmeldeversuche je Client-Adresse und Minute (0 = aus)
    auth_rate_limit_per_minute: int = Field(default=30, ge=0, le=10_000)

    # --- Connectoren ---------------------------------------------------------
    #: Öffentliche Basis-URL der Instanz (ohne Pfad), z. B. https://music.example.com.
    #: Daraus entsteht die OAuth-Redirect-URI {public_url}/api/oauth/callback/{plattform}.
    public_url: str | None = None
    #: Schlüssel für die Verschlüsselung der OAuth-Tokens (Fernet, 32 Byte Base64).
    #: Mehrere Schlüssel kommagetrennt: der erste verschlüsselt, alle entschlüsseln (Rotation).
    token_encryption_key: SecretStr | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: SecretStr | None = None
    #: Google-OAuth-Client (Typ „Webanwendung“) für die YouTube Data API v3
    youtube_client_id: str | None = None
    youtube_client_secret: SecretStr | None = None
    #: Nur mit eigener SoundCloud-API-App (setzt laut SoundCloud Artist Pro voraus)
    soundcloud_client_id: str | None = None
    soundcloud_client_secret: SecretStr | None = None
    #: Plattformen, auf die die Synchronisierung schreiben darf (kommagetrennt, z. B.
    #: "spotify,youtube"). Leer = nur Vorschau (Dry-Run), nichts wird verändert.
    sync_write_platforms: str = ""
    #: Wie oft der Worker nach fälligen geplanten Synchronisierungen sucht (Sekunden)
    sync_scheduler_interval_seconds: float = Field(default=60.0, ge=5, le=3600)
    #: Höchstzahl Plattform-Suchen für nicht verknüpfte Tracks je Lauf
    sync_lookup_limit: int = Field(default=50, ge=0, le=500)
    #: Zeitlimit je Plattform-Anfrage in Sekunden
    connector_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # --- Import ---------------------------------------------------------------
    import_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    import_max_rows: int = Field(default=20_000, ge=1, le=200_000)

    # --- Hintergrundjobs -----------------------------------------------------
    worker_poll_interval_seconds: float = 2.0
    worker_max_attempts: int = 5

    @property
    def oidc_expected_audience(self) -> str | None:
        return self.oidc_audience or self.oidc_client_id

    @property
    def oidc_login_enabled(self) -> bool:
        """Browser-Anmeldung (Authorization Code Flow) ist vollständig konfiguriert."""
        return bool(
            self.auth_mode is AuthMode.OIDC
            and self.oidc_issuer
            and self.oidc_client_id
            and self.oidc_client_secret
            and self.public_url
        )

    @property
    def oidc_redirect_uri(self) -> str | None:
        return f"{self.public_url.rstrip('/')}/api/auth/callback" if self.public_url else None

    @property
    def session_cookie_secure_effective(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return bool(self.public_url and self.public_url.lower().startswith("https://"))

    @property
    def api_docs_enabled(self) -> bool:
        return self.api_docs if self.api_docs is not None else self.app_env != AppEnv.PRODUCTION

    def sync_write_enabled(self, platform: str) -> bool:
        allowed = {p.strip().lower() for p in self.sync_write_platforms.split(",") if p.strip()}
        return platform.lower() in allowed

    @property
    def oauth_redirect_base(self) -> str | None:
        return f"{self.public_url.rstrip('/')}/api/oauth/callback" if self.public_url else None

    @model_validator(mode="after")
    def _validate_auth(self) -> Settings:
        if self.auth_mode is AuthMode.DEV:
            if self.app_env is AppEnv.PRODUCTION:
                raise ValueError(
                    "MUSICHUB_AUTH_MODE=dev ist in production nicht erlaubt. "
                    "Nutze MUSICHUB_AUTH_MODE=oidc."
                )
            token = self.dev_auth_token.get_secret_value() if self.dev_auth_token else ""
            if len(token) < 16:
                raise ValueError(
                    "MUSICHUB_DEV_AUTH_TOKEN muss im dev-Modus gesetzt sein (mind. 16 Zeichen)."
                )
        if self.auth_mode is AuthMode.OIDC and not (
            self.oidc_issuer and self.oidc_expected_audience
        ):
            raise ValueError(
                "Für MUSICHUB_AUTH_MODE=oidc sind MUSICHUB_OIDC_ISSUER und "
                "MUSICHUB_OIDC_CLIENT_ID erforderlich."
            )
        if self.public_url:
            url = self.public_url.lower()
            if not url.startswith(("http://", "https://")) or self.public_url.count("/") > 3:
                raise ValueError(
                    "MUSICHUB_PUBLIC_URL muss eine Basis-URL ohne Pfad sein, "
                    "z. B. https://music.example.com"
                )
        if self.app_env is AppEnv.PRODUCTION:
            problems: list[str] = []
            if self.auth_mode is AuthMode.OIDC and not self.oidc_login_enabled:
                problems.append(
                    "MUSICHUB_OIDC_CLIENT_ID, MUSICHUB_OIDC_CLIENT_SECRET und "
                    "MUSICHUB_PUBLIC_URL sind für die Anmeldung erforderlich"
                )
            if self.public_url and not self.public_url.lower().startswith("https://"):
                problems.append("MUSICHUB_PUBLIC_URL muss mit https:// beginnen")
            if self.session_cookie_secure is False:
                problems.append("MUSICHUB_SESSION_COOKIE_SECURE=false ist nicht erlaubt")
            if "*" in self.cors_origins:
                problems.append("MUSICHUB_CORS_ORIGINS darf nicht * enthalten")
            if problems:
                raise ValueError("Unsichere production-Konfiguration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
