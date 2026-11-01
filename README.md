# MusicHub

Selbst gehostete, plattformunabhängige Musikverwaltung für **Spotify, SoundCloud und YouTube**.

MusicHub hält eine zentrale Musiksammlung in einer eigenen PostgreSQL-Datenbank und
synchronisiert daraus Playlists auf die Streamingplattformen. Die **lokale Datenbank ist
die führende Instanz**: Ein Track existiert auch dann, wenn er auf keiner oder nur einer
Plattform verfügbar ist. Fällt eine Plattform aus, laufen Sammlung und alle anderen
Plattformen ungestört weiter.

> **Stand: Etappe 3 – bereit für den Produktivbetrieb.** Bibliothek, Import/Export,
> Matching, Plattform-Konten und Synchronisierung nach Spotify/YouTube; Anmeldung über
> Authentik (OIDC) mit serverseitiger Sitzung; gehärtete `compose.yaml`, Backup-Skripte.
> Prüfergebnis und offene Punkte: [docs/PRUEFBERICHT.md](docs/PRUEFBERICHT.md),
> Etappen: [docs/ROADMAP.md](docs/ROADMAP.md).

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
- **Import und Export:** Assistent für TuneMyMusic-, Soundiiz-, Exportify- und eigene
  CSV-Dateien, JSON und Textlisten mit Vorschau, Duplikaterkennung und Importbericht; Export
  der Bibliothek oder einzelner Playlists als CSV/JSON, wieder importierbar ohne Duplikate
  ([Details](docs/IMPORT_EXPORT.md))
- **Matching:** erkennt doppelte Tracks über Plattformen hinweg (z. B. Spotify „Original Mix“,
  SoundCloud ohne Zusatz, YouTube „[Official Audio]“) mit nachvollziehbarem Confidence Score
  0–100 und Gründen; Remixes, Edits, Live- und andere Versionen werden nie automatisch
  zusammengeführt. Bestätigen, Ablehnen, Später prüfen, manueller Vergleich, Verlauf mit
  Rückgängig ([Details](docs/MATCHING.md))
- **Plattformen:** Spotify- und YouTube-Konto per OAuth verbinden (SoundCloud vorbereitet,
  braucht Artist Pro), eigene Playlists auswählen und im Hintergrund importieren –
  mit Fortschritt, Wiederholung bei Rate-Limits und inkrementeller Aktualisierung. Rein
  lesend, Tokens nur verschlüsselt ([Details](docs/CONNECTORS.md))
- **Synchronisierung:** Playlists nach Spotify und YouTube übertragen – nur hinzufügen
  oder (für von MusicHub angelegte Playlists) vollständig abgleichen, manuell oder geplant,
  mit Vorschau/Dry-Run, Konflikterkennung, Liste nicht übertragbarer Tracks und Historie.
  Schreiben erst nach ausdrücklicher Freigabe ([Details](docs/SYNC.md))
- **Einstellungen:** Anmeldung, helles/dunkles Schema, Tag-Verwaltung

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
| Plattform-Connectoren | Spotify, YouTube, SoundCloud (lesend), Registry mit Fehlerisolation | `…/connectors` |
| Plattform-Konten | OAuth, verschlüsselte Tokens, Import-Jobs | `…/connections` |
| Matching-Engine | reine Funktionen (Versionserkennung, Confidence 0–100, Blocking) | `…/matching` |
| Synchronisation | reine Planung + Ziele, Vorschau, Ausführung als Job | `…/sync` |
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

Schritt für Schritt (Authentik, Nginx Proxy Manager, `.env`, Prüfung):
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. Kurzfassung:

1. In Authentik eine OAuth2/OpenID-Anwendung `musichub` anlegen (Confidential, Signing Key,
   Redirect-URI `https://<domain>/api/auth/callback`).
2. `.env` aus `.env.example` erstellen (`chmod 600`), mindestens `MUSICHUB_PUBLIC_URL`,
   `POSTGRES_PASSWORD`, `MUSICHUB_OIDC_ISSUER`, `MUSICHUB_OIDC_CLIENT_ID`,
   `MUSICHUB_OIDC_CLIENT_SECRET` setzen.
3. `docker compose up -d`, Proxy Host in Nginx Proxy Manager auf das Frontend (Port 8080).
4. Prüfen: `https://<domain>/api/health/ready` liefert `"status":"ok"`, Anmelden im Browser.

Das Backend wartet beim Start auf die Datenbank und migriert sie automatisch (geschützt
durch einen PostgreSQL-Advisory-Lock). Der Worker beginnt erst, wenn das Schema aktuell
ist. Deshalb ist die Reihenfolge egal, in der Container neu gestartet werden. Ein
unsicher konfiguriertes `production`-Backend startet nicht.

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

```bash
scripts/backup.sh                       # Datenbank + .env/Compose-Dateien nach ./backups
scripts/restore.sh backups/<datei>.dump # Wiederherstellen (mit Sicherheitskopie)
```

Automatisierung, Auslagerung, Upgrade und Rollback: [docs/BACKUP.md](docs/BACKUP.md).

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

Lesende API-Endpunkte unter `/api/v1` brauchen eine Anmeldung, schreibende zusätzlich
Schreibrechte. Öffentlich sind nur `/api/health/*` und `/api/auth/*` (Anmeldung).

| Modus | Wann | Verhalten |
|---|---|---|
| `oidc` | Betrieb | Browser: Anmeldung über Authentik (Authorization Code Flow + PKCE), serverseitige Sitzung im HttpOnly-Cookie, CSRF-Schutz, Ablauf nach Inaktivität und absolut, Abmeldung. Skripte: JWT-Access-Tokens von Authentik als Bearer. Schreibrechte über Gruppe `MUSICHUB_OIDC_WRITE_GROUP`. |
| `dev` | lokale Entwicklung | Statisches Bearer-Token aus `MUSICHUB_DEV_AUTH_TOKEN` (≥ 16 Zeichen), Eingabe unter *Einstellungen*. Start wird bei `MUSICHUB_APP_ENV=production` verweigert. |

Einrichtung in Authentik: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#2-authentik-einrichten),
Sicherheitsmerkmale: [docs/SECURITY.md](docs/SECURITY.md).

### Plattform-Konten (optional)

Für Spotify/YouTube in `.env` setzen: `MUSICHUB_PUBLIC_URL` (Adresse, unter der der
Browser MusicHub aufruft), `MUSICHUB_TOKEN_ENCRYPTION_KEY` (32 Byte Base64) und die
Client-ID/-Secret der jeweiligen App. Redirect-URI in der App der Plattform:
`<PUBLIC_URL>/api/oauth/callback/<plattform>` – Spotify verlangt dafür HTTPS oder
`http://127.0.0.1:<port>`. Voraussetzungen (Spotify Premium für den App-Besitzer, YouTube-
Kontingent, SoundCloud Artist Pro) und Schritt-für-Schritt-Anleitung:
[docs/CONNECTORS.md](docs/CONNECTORS.md). Ohne diese Werte bleibt alles andere nutzbar.
Schreiben auf Plattformen (Synchronisierung) zusätzlich mit
`MUSICHUB_SYNC_WRITE_PLATFORMS=spotify,youtube` freischalten und das Konto auf *Plattformen*
mit Schreibzugriff verbinden – siehe [docs/SYNC.md](docs/SYNC.md).

## Weitere Dokumentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – Komponenten, Abhängigkeiten, Fehlerisolation
- [docs/DATABASE.md](docs/DATABASE.md) – Schema, Konventionen, Migrationen
- [docs/API.md](docs/API.md) – Endpunkte, Auth, Fehlerformat
- [docs/IMPORT_EXPORT.md](docs/IMPORT_EXPORT.md) – Importformate, Spalten, Ablauf, Export
- [docs/CONNECTORS.md](docs/CONNECTORS.md) – Plattform-APIs, Einrichtung, Scopes, Kontingente
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) – Produktivbetrieb mit Authentik und Nginx Proxy Manager
- [docs/BACKUP.md](docs/BACKUP.md) – Backup, Wiederherstellung, Upgrade, Rollback
- [docs/SECURITY.md](docs/SECURITY.md) – Sicherheitsmaßnahmen und ihre Tests
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) – Fehlersuche
- [docs/PRUEFBERICHT.md](docs/PRUEFBERICHT.md) – Prüfergebnis, offene Punkte, API-Grenzen
- [docs/SYNC.md](docs/SYNC.md) – Synchronisierung: Freigaben, Modi, Konflikte, Historie
- [docs/MATCHING.md](docs/MATCHING.md) – Erkennung gleicher Tracks, Score, Regeln, Zusammenführen
- [docs/ROADMAP.md](docs/ROADMAP.md) – Etappen und offene Punkte
- [CLAUDE.md](CLAUDE.md) – Projektkontext und Regeln für Claude Code
