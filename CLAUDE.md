# CLAUDE.md – MusicHub

Zentrale Orientierung für Claude Code. Vor jeder Änderung lesen; bei Architekturfragen
zusätzlich `docs/ARCHITECTURE.md`, bei Schemaänderungen `docs/DATABASE.md`.

## Projekt in einem Satz

Selbst gehostete Musikverwaltung: eine lokale Sammlung in PostgreSQL ist die **führende
Instanz**, Playlists werden von dort nach Spotify, SoundCloud und YouTube synchronisiert.

## Nicht verhandelbare Regeln

1. **Lokal führt.** Kein Code darf lokale Daten löschen oder überschreiben, nur weil eine
   Plattform etwas anderes meldet. Sync-Richtung ist lokal → Plattform.
2. **Track ohne Plattform ist gültig.** Niemals einen Plattform-Link voraussetzen.
3. **Connector-Aufrufe nur über `ConnectorRegistry.call()`.** Nie einen Connector direkt
   aus API oder Jobs aufrufen. Plattformübergreifende Arbeit = ein Job pro Plattform.
4. **Connectoren greifen nicht auf die Datenbank zu** und liefern nur DTOs aus
   `connectors/base.py`. Fehler immer als `ConnectorError` mit korrektem `retryable`.
5. **Keine ungeschützten Schreiboperationen.** Lesende `/api/v1`-Routen:
   `Depends(current_principal)`. Schreibende: `Depends(require_write)`.
   `tests/test_api.py` prüft das für alle Routen – nicht abschwächen.
6. **Keine Geheimnisse im Repo.** Neue Einstellungen in `core/config.py` (Präfix
   `MUSICHUB_`) und in `.env.example` mit leerem/Platzhalterwert dokumentieren.
7. **Schemaänderung = Modell + Alembic-Migration im selben Commit.** CI führt
   `alembic check` aus.
8. **Matching und Sync bleiben reine Funktionen** (kein I/O) – I/O gehört in Jobs/Services.

## Struktur

```
backend/
  src/musichub/
    main.py            App-Fabrik create_app(), Router-Registrierung
    serve.py           Container-Start: DB abwarten, migrieren, uvicorn
    core/              config (Settings), logging, errors (RFC 9457), middleware (Request-ID)
    auth/              Principal, Verifier (dev, oidc), Dependencies
    domain/types.py    Platform, LinkStatus, MatchMethod, JobStatus
    db/                Engine/Session, Basis-Modell, Migrations-Status, migrate (Advisory-Lock)
    models/            SQLAlchemy-Modelle (library.py, job.py) – in models/__init__ exportieren
    migrations/        Alembic (env.py, versions/0001_…)
    api/               schemas.py (Pydantic), routes/ (health, system, tracks)
    connectors/        base.py (Schnittstelle, DTOs, Fehler), platforms.py, registry.py
    matching/engine.py Score-Berechnung
    sync/engine.py     plan_playlist_sync
    jobs/              queue.py, handlers.py (@handler), worker.py
  tests/               pytest; DB-Tests mit @pytest.mark.db
frontend/
  src/lib/             api.ts (Client), auth.ts (TokenProvider), types.ts, session.ts
  src/pages/           index.astro (Übersicht), library.astro (Sammlung)
  src/layouts/         Base.astro (Layout + globale Styles)
  nginx/               nginx-Konfiguration (statisch + /api-Proxy)
compose.yaml           Betrieb/Arcane (GHCR-Images)
compose.dev.yaml       Entwicklung (Build, Hot Reload)
.github/workflows/ci.yml
docs/                  ARCHITECTURE, DATABASE, API, ROADMAP
```

## Befehle

```bash
# Backend (in backend/)
uv sync
uv run pytest                                   # DB-Tests: TEST_DATABASE_URL setzen
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "…"   # MUSICHUB_DATABASE_URL setzen
uv run alembic upgrade head && uv run alembic check
uv run uvicorn musichub.main:app --reload
uv run musichub-worker

# Frontend (in frontend/)
pnpm install
pnpm test          # Vitest
pnpm build         # astro check + build
pnpm dev           # Proxy /api -> MUSICHUB_API_URL (Standard http://localhost:8000)

# Ganzer Stack
docker compose -f compose.dev.yaml up --build
```

## Konventionen

- **Sprache:** Code-Bezeichner Englisch; Docstrings, Kommentare, Logmeldungen,
  Fehlertexte, UI und Doku Deutsch.
- **Python:** 3.13, Typannotationen überall, `from __future__ import annotations`,
  ruff (Zeilenlänge 100). SQLAlchemy 2-Stil (`Mapped`, `select()`), synchrone Sessions.
  Pydantic-Schemas getrennt von ORM-Modellen.
- **Fehler:** fachliche Fehler als `AppError`-Unterklassen (`core/errors.py`), keine
  `HTTPException` in Fachlogik. Keine internen Details in Antworten.
- **Logging:** `logging.getLogger(__name__)`, strukturierte Zusatzfelder per `extra={…}`,
  keine Tokens/Passwörter loggen.
- **Aufzählungen:** in `domain/types.py`, in der DB als Text + CHECK (`sql_in()`).
- **Jobs:** Handler idempotent schreiben (Jobs können wiederholt laufen).
  Endgültige Fehler: `JobError(retryable=False)`.
- **Frontend:** TypeScript strict (`astro/tsconfigs/strictest`), keine zusätzlichen
  Frameworks ohne Grund (React nur, wenn eine Komponente echten Zustand braucht).
  API-Zugriff nur über `ApiClient`; Typen in `lib/types.ts` mit `api/schemas.py` synchron halten.
  Dynamisch erzeugte DOM-Elemente bekommen kein Astro-Scope-Attribut → Styles dafür
  über `:global(...)` unter einer Wrapper-Klasse.
- **Abhängigkeiten:** sparsam. Neue Pakete begründen; kein Redis/Celery, die Job-Queue
  liegt bewusst in PostgreSQL.
- **Tests:** Jede neue Logik mit Tests. Connectoren gegen aufgezeichnete Antworten testen,
  nie gegen echte Plattformen in der CI.

## Deployment-Hinweise

- Images: `ghcr.io/ex0th/musichub-backend` (API + Worker), `ghcr.io/ex0th/musichub-frontend`.
- Der Betrieb läuft über **Arcane**: `compose.yaml` ohne YAML-Anker, `include` oder
  `extends` halten, damit Arcane sie aktualisieren kann.
- Backend migriert beim Start; daher Migrationen **abwärtskompatibel** gestalten (alter
  Worker kann kurz neben neuem Schema laufen): erst Spalten hinzufügen, später entfernen.
- Container laufen als Nicht-Root mit Read-only-Root-FS; Schreibzugriffe nur nach `/tmp`.

## Arbeitsweise

- Vor Änderungen bestehende Dateien lesen und nichts Fremdes überschreiben.
- Nach Änderungen: Backend-Tests, ruff, Frontend-Tests und -Build ausführen.
- `docs/ROADMAP.md` aktualisieren, wenn eine Etappe oder ein offener Punkt erledigt ist;
  `docs/API.md` bei neuen Endpunkten.
