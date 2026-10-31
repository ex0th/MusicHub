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
    cors_origins: list[str] = Field(default_factory=list)

    # --- Authentifizierung ---------------------------------------------------
    auth_mode: AuthMode = AuthMode.DEV
    dev_auth_token: SecretStr | None = None
    dev_auth_user: str = "dev"

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    #: Optional; sonst aus {issuer}/.well-known/openid-configuration ermittelt.
    oidc_jwks_url: str | None = None
    #: Gruppe/Rolle, die Schreibrechte bekommt. Leer = jeder angemeldete Nutzer.
    oidc_write_group: str | None = None
    oidc_groups_claim: str = "groups"

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
    #: Zeitlimit je Plattform-Anfrage in Sekunden
    connector_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # --- Import ---------------------------------------------------------------
    import_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    import_max_rows: int = Field(default=20_000, ge=1, le=200_000)

    # --- Hintergrundjobs -----------------------------------------------------
    worker_poll_interval_seconds: float = 2.0
    worker_max_attempts: int = 5

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
        if self.auth_mode is AuthMode.OIDC and not (self.oidc_issuer and self.oidc_audience):
            raise ValueError(
                "Für MUSICHUB_AUTH_MODE=oidc sind MUSICHUB_OIDC_ISSUER und "
                "MUSICHUB_OIDC_AUDIENCE erforderlich."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
