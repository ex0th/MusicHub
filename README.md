# MusicHub

Selbst gehostete, plattformunabhängige Musikverwaltung für **Spotify, SoundCloud und YouTube**.

MusicHub hält eine zentrale Musiksammlung in einer eigenen PostgreSQL-Datenbank und
synchronisiert daraus Playlists auf die Streamingplattformen. Die **lokale Datenbank ist
die führende Instanz**: Ein Track existiert auch dann, wenn er auf keiner oder nur einer
Plattform verfügbar ist. Fällt eine Plattform aus, laufen Sammlung und alle anderen
Plattformen ungestört weiter.

> **Stand: Etappe 2 – Musikbibliothek.** Tracks, Plattform-Quellen, Tags und Playlists lassen
> sich vollständig in der Weboberfläche und per REST-API verwalten – ganz ohne Plattform-Konten.
> Links zu Spotify, SoundCloud und YouTube werden beim Einfügen erkannt. Die automatische
> Anbindung der Plattform-APIs folgt in den nächsten Etappen, siehe [docs/ROADMAP.md](docs/ROADMAP.md).

## Funktionen

- **Dashboard:** Anzahl Tracks und Playlists, Verfügbarkeit je Plattform, Tracks, die nur auf
  einer Plattform verfügbar sind
- **Musikbibliothek:** Tabelle mit Suche, Filtern (Plattform, fehlt auf, nur lokal, Tag),
  Sortierung per Spaltenkopf, Abdeckungsstreifen mit direkten Links zu den Plattformen,
  neue Tracks samt Links und Tags anlegen
- **Track-Detail:** alle Angaben bearbeiten, Plattform-Links verknüpfen und als (nicht)
  verfügbar markieren, Tags, Playlist-Zugehörigkeit
- **Playlists:** anlegen, umbenennen, Tracks suchen und hinzufügen, per Drag & Drop oder
  Pfeiltasten umsortieren, entfernen
- **Plattformen und Einstellungen:** Zahlen je Plattform, Anmeldung, helles/dunkles Schema,
  Tag-Verwaltung

## Inhalt

- [Komponenten](#komponenten)
- [Betrieb mit Docker Compose und Arcane](#betrieb-mit-docker-compose-und-arcane)
- [Lokale Entwicklung](#lokale-entwicklung)
- [Tests](#tests)
- [Konfiguration](#konfiguration)
- [Weitere Dokumentation](#weitere-dokumentation)

## Komponenten

| Komponente | Technik | Ort |
|---|---|---|
| Frontend | Astro, TypeScript, statisch ausgeliefert über nginx | `frontend/` |
| Backend/API | Python 3.13, FastAPI, Pydantic, SQLAlchemy 2 | `backend/src/musichub/api` |
| Datenbank | PostgreSQL 17, Migrationen mit Alembic | `backend/src/musichub/models`, `…/migrations` |
| Plattform-Connectoren | gemeinsame Schnittstelle + Registry mit Fehlerisolation | `…/connectors` |
| Matching-Engine | reine Funktionen (ISRC, Fuzzy-Score) | `…/matching` |
| Synchronisations-Engine | reine Sync-Planung lokal → Plattform | `…/sync` |
| Hintergrundjobs | Job-Queue in PostgreSQL, eigener Worker-Container | `…/jobs` |

Im Betrieb laufen vier Container: `db`, `backend` (API), `worker` und `frontend`
(nginx mit statischem Frontend, leitet `/api` an das Backend weiter). Nur `frontend`
veröffentlicht einen Port.

## Betrieb mit Docker Compose und Arcane

Die GitHub Action baut bei jedem Push auf `main` Multi-Arch-Images (amd64/arm64), testet
den kompletten Stack mit `compose.yaml` und veröffentlicht:

- `ghcr.io/ex0th/musichub-backend` (API und Worker)
- `ghcr.io/ex0th/musichub-frontend`

Tags: `latest` (main), `sha-<commit>`, bei Git-Tags `v1.2.3` zusätzlich `v1.2.3` und `1.2`.

### Einrichtung

1. `compose.yaml` und `.env.example` auf den Server bringen (oder in Arcane ein neues
   Projekt anlegen und `compose.yaml` einfügen).
2. `.env` aus `.env.example` erstellen und mindestens setzen:
   - `POSTGRES_PASSWORD` – z. B. `openssl rand -hex 24`
   - Authentifizierung, siehe [Konfiguration](#konfiguration)
   - `MUSICHUB_HTTP_BIND`/`MUSICHUB_HTTP_PORT` passend zum Reverse Proxy
3. Starten: `docker compose up -d` (bzw. in Arcane deployen).
4. Prüfen: `http://<host>:8080/api/health/ready` liefert `"status":"ok"`.

Das Backend wartet beim Start auf die Datenbank und migriert sie automatisch (geschützt
durch einen PostgreSQL-Advisory-Lock). Der Worker beginnt erst, wenn das Schema aktuell
ist. Deshalb ist die Reihenfolge egal, in der Container neu gestartet werden.

### Automatische Updates mit Arcane

Zwei Varianten, beide funktionieren mit dieser `compose.yaml`:

1. **Image Update Watcher (einfach):** In Arcane unter *Environments → Umgebung →
   Automations → Updates* den *Image Update Watcher* und *Auto Update* aktivieren. Arcane
   erkennt neue `latest`-Images und erstellt nur die geänderten Services neu.
2. **Webhook (sofort):** In Arcane unter *Settings → Webhooks* einen Webhook für das
   Projekt anlegen und die URL als Repository-Secret `ARCANE_WEBHOOK_URL` in GitHub
   hinterlegen. Die Action ruft ihn nach jedem erfolgreichen Build auf `main` auf. Setzt
   voraus, dass Arcane aus dem Internet erreichbar ist.

Die Datenbank ist per Label `com.getarcaneapp.arcane.updater=false` von automatischen
Updates ausgenommen: PostgreSQL-Major-Upgrades brauchen Dump/Restore.

Ist das GHCR-Paket privat, in Arcane unter *Container Registries* `ghcr.io` mit einem
GitHub-Token (Scope `read:packages`) hinterlegen – oder die Pakete auf GitHub öffentlich stellen.

Für reproduzierbare Stände `MUSICHUB_VERSION=v0.1.0` in `.env` setzen statt `latest`.

### Backup

Alle Daten liegen im Volume `postgres-data`:

```bash
docker compose exec db pg_dump -U musichub -Fc musichub > musichub-$(date +%F).dump
```

## Lokale Entwicklung

### Variante A: alles in Docker (Hot Reload)

```bash
docker compose -f compose.dev.yaml up --build
```

- Frontend: <http://localhost:4321> – unter *Einstellungen* mit dem Token
  `local-dev-token-change-me` anmelden (oder eigenes `MUSICHUB_DEV_AUTH_TOKEN` in `.env`)
- API und Swagger-UI: <http://localhost:8000/api/docs>
- PostgreSQL: `localhost:5432`, Benutzer/Passwort `musichub`

Änderungen unter `backend/src` und `frontend/src` werden sofort übernommen.

### Variante B: nativ

Voraussetzungen: Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22, pnpm 10, PostgreSQL.

```bash
# Datenbank z. B. nur den db-Service aus compose.dev.yaml starten
docker compose -f compose.dev.yaml up -d db

# Backend
cd backend
uv sync
export MUSICHUB_DATABASE_URL=postgresql+psycopg://musichub:musichub@localhost:5432/musichub
export MUSICHUB_DEV_AUTH_TOKEN=local-dev-token-change-me
uv run musichub-migrate
uv run uvicorn musichub.main:app --reload       # API auf :8000
uv run musichub-worker                          # zweites Terminal

# Frontend
cd frontend
pnpm install
pnpm dev                                        # :4321, leitet /api an :8000 weiter
```

## Tests

```bash
# Backend – DB-Tests brauchen eine leere Test-Datenbank
cd backend
TEST_DATABASE_URL=postgresql+psycopg://musichub:musichub@localhost:5432/musichub_test uv run pytest
uv run ruff check . && uv run ruff format --check .

# Frontend
cd frontend
pnpm test        # Vitest
pnpm build       # astro check (Typen) + Build
```

Ohne `TEST_DATABASE_URL` laufen nur die Tests ohne Datenbank; DB-Tests werden übersprungen.

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen, dokumentiert in [`.env.example`](.env.example).

### Authentifizierung

Lesende API-Endpunkte unter `/api/v1` brauchen ein gültiges Token, schreibende zusätzlich
Schreibrechte. Öffentlich sind nur `/api/health/*` und die OpenAPI-Beschreibung.

| Modus | Wann | Verhalten |
|---|---|---|
| `dev` | lokale Entwicklung | Statisches Bearer-Token aus `MUSICHUB_DEV_AUTH_TOKEN` (≥ 16 Zeichen). Start wird bei `MUSICHUB_APP_ENV=production` verweigert. |
| `oidc` | Betrieb | Validiert JWT-Access-Tokens (Signatur über JWKS, `iss`, `aud`, `exp`). Schreibrechte über Gruppe `MUSICHUB_OIDC_WRITE_GROUP`. |

Für **Authentik**: einen OAuth2/OpenID-Provider und eine Anwendung `musichub` anlegen,
`MUSICHUB_OIDC_ISSUER` auf die Issuer-URL (`https://auth.example.com/application/o/musichub/`)
und `MUSICHUB_OIDC_AUDIENCE` auf die Client-ID setzen. Der Login-Flow im Frontend folgt
in einer späteren Etappe, die API ist dafür bereits fertig.

> Wer die Instanz vorher im Netz erreichbar machen will: Bis der OIDC-Login im Frontend
> existiert, den Zugang über den Reverse Proxy absichern (z. B. Authentik-Forward-Auth)
> oder `MUSICHUB_HTTP_BIND=127.0.0.1` lassen.

## Weitere Dokumentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – Komponenten, Abhängigkeiten, Fehlerisolation
- [docs/DATABASE.md](docs/DATABASE.md) – Schema, Konventionen, Migrationen
- [docs/API.md](docs/API.md) – Endpunkte, Auth, Fehlerformat
- [docs/ROADMAP.md](docs/ROADMAP.md) – Etappen und offene Punkte
- [CLAUDE.md](CLAUDE.md) – Projektkontext und Regeln für Claude Code
