# Architektur

## Leitprinzipien

1. **Die lokale Datenbank ist die führende Instanz.** Tracks, Playlists und ihre
   Reihenfolge leben in PostgreSQL. Plattformen sind Ziele (und später Importquellen),
   nie die Wahrheit.
2. **Ein Track braucht keine Plattform.** Plattform-Zuordnungen hängen als
   `PlatformLink` am Track. Ein Track ohne Links ist ein vollwertiger Track.
3. **Fehlerisolation pro Plattform.** Ein Fehler bei SoundCloud darf weder Spotify noch
   die lokale Verwaltung beeinträchtigen (siehe [Connectoren](#connectoren-und-fehlerisolation)).
4. **Reine Logik ohne I/O.** Matching und Sync-Planung sind reine Funktionen und ohne
   Netzwerk oder Datenbank testbar.
5. **Wenig Infrastruktur.** Kein Redis, kein Celery, kein Message Broker: Jobs liegen in
   PostgreSQL. Vier Container insgesamt.

## Überblick

```
                 ┌──────────────────────────── frontend (nginx :8080) ─┐
 Browser ───────►│  statisches Astro-Frontend      /api/* ──► Proxy    │
                 └─────────────────────────────────────────┬───────────┘
                                                           │
                 ┌───────────────── backend (FastAPI :8000) ▼──────────┐
                 │ api/routes ── auth ── Services/Abfragen             │
                 │      │                                              │
                 │      ├── matching (rein)    sync (rein)             │
                 │      └── ConnectorRegistry ──► Spotify | SoundCloud │
                 │                                 | YouTube           │
                 └───────────────┬─────────────────────────────────────┘
                                 │ SQLAlchemy 2 / psycopg 3
                 ┌───────────────▼───────────┐        ┌─────────────────┐
                 │ db (PostgreSQL 17)        │◄───────│ worker          │
                 │ tracks, platform_links,   │  Jobs  │ jobs.worker     │
                 │ playlists, jobs, …        │        │ + Registry      │
                 └───────────────────────────┘        └─────────────────┘
```

## Komponenten

| # | Komponente | Paket | Verantwortung | Darf abhängen von |
|---|---|---|---|---|
| 1 | Frontend | `frontend/` | UI, ruft nur die REST-API auf | API (HTTP) |
| 2 | Backend/API | `musichub.api`, `musichub.main` | HTTP, Validierung, Auth, Transaktionen | alle Backend-Pakete |
| 3 | Datenbank | `musichub.models`, `musichub.db`, `musichub/migrations` | Schema, Sessions, Migrationen | `domain` |
| 4 | Connectoren | `musichub.connectors` | Plattform-APIs, liefern neutrale DTOs | `domain`, `core` – **nicht** `db` |
| 5 | Matching-Engine | `musichub.matching` | Track-Ähnlichkeit bewerten | `domain` |
| 6 | Synchronisations-Engine | `musichub.sync` | Soll/Ist-Abgleich planen | `domain` |
| 7 | Hintergrundjobs | `musichub.jobs` | Queue, Worker, Job-Handler | alle außer `api` |

Querschnitt: `musichub.core` (Konfiguration, Logging, Fehler, Middleware),
`musichub.auth`, `musichub.domain` (gemeinsame Aufzählungstypen).

Abhängigkeitsrichtung: `api`/`jobs` → `matching`/`sync`/`connectors`/`models` → `domain`/`core`.
Connectoren, Matching und Sync kennen sich gegenseitig nicht; erst Jobs/Services
verbinden sie.

## Connectoren und Fehlerisolation

Jeder Connector implementiert `PlatformConnector` (`connectors/base.py`) und liefert
`RemoteTrack`/`RemotePlaylist`. Aufrufe laufen **ausschließlich** über
`ConnectorRegistry.call(platform, fn)`:

- Jede Ausnahme wird zu `ConnectorError` mit `platform` und `retryable`.
  Fremde Ausnahmen (`httpx`, `KeyError` aus kaputtem JSON …) werden eingekapselt.
- **Circuit Breaker pro Plattform**: nach 3 wiederholbaren Fehlern in Folge wird die
  Plattform 60 s nicht angesprochen (`ConnectorUnavailableError`). Andere Plattformen
  laufen weiter.
- `ConnectorNotConfiguredError` / `ConnectorNotImplementedError` sind nicht wiederholbar
  und zählen nicht als Störung.
- Status je Plattform: `not_configured`, `ready`, `degraded`, `suspended`
  (`GET /api/v1/connectors`).

Der Breaker-Zustand liegt im Speicher des jeweiligen Prozesses (API und Worker getrennt).
Plattformübergreifende Vorgänge (z. B. "Playlist auf alle Ziele synchronisieren") werden
als **ein Job pro Plattform** eingestellt, sodass ein Fehlschlag nur diesen Job betrifft.

## Matching

`matching/engine.py` bewertet lokal ↔ Plattform:

1. Gleiche ISRC → Score 1.0 (`isrc`).
2. Sonst Fuzzy-Score: Titel 50 %, Interpret 35 %, Dauer 15 % (fehlt die Dauer, wird
   neu gewichtet). Titel werden normalisiert (Akzente, „feat.“, „(Official Video)“,
   „Artist - Title“ von YouTube, „- Topic“-Kanäle).
3. Unterschiedliche Versions-Marker (Remix, Live, Acoustic …) → Score × 0.6. Ein Remix wird
   so nie automatisch als Original verknüpft.

Ab 0.92 darf automatisch verknüpft werden, ab 0.6 wird ein `candidate`-Link vorgeschlagen.

## Synchronisation

Richtung: **lokal → Plattform**. `sync/engine.py::plan_playlist_sync` erhält die lokale
Soll-Liste (mit externen IDs aus bestätigten Links) und den Ist-Zustand der Plattform und
liefert einen `SyncPlan` (Soll-Reihenfolge, hinzufügen, entfernen, Reihenfolge geändert,
nicht auflösbare Tracks). Tracks ohne Link werden übersprungen und gemeldet – lokal
bleiben sie vollständig erhalten. Ausgeführt wird der Plan von einem Job.

## Hintergrundjobs

- Tabelle `jobs`, abgeholt mit `SELECT … FOR UPDATE SKIP LOCKED` → mehrere Worker möglich.
- Status: `queued` → `running` → `succeeded` | `failed` (Retry mit Backoff 30 s · 2ⁿ, max. 1 h)
  | `dead` (endgültig oder `max_attempts` erreicht).
- Hängende Jobs (`running` > 15 min) werden neu vergeben.
- Worker beendet sich bei SIGTERM nach dem aktuellen Job (`stop_grace_period: 60s`).
- Neuer Job-Typ: Funktion in `jobs/handlers.py` mit `@handler("bereich.aktion")`.

## Authentifizierung

`auth/verifiers.py` enthält zwei Verifier hinter dem Protokoll `TokenVerifier`:

- `DevTokenVerifier` – zeitkonstanter Vergleich mit `MUSICHUB_DEV_AUTH_TOKEN`.
- `OidcVerifier` – JWT-Prüfung über JWKS (Discovery über
  `{issuer}/.well-known/openid-configuration`, Schlüssel gecacht, Key-Rotation).

Endpunkte hängen nur von `current_principal` (lesen) bzw. `require_write` (schreiben) ab.
Ein Test (`tests/test_api.py`) prüft für **alle** Routen, dass schreibende Methoden
`require_write` und alle `/api/v1`-Routen eine Anmeldung verlangen.

Das Frontend kapselt die Token-Herkunft in `TokenProvider` (`frontend/src/lib/auth.ts`);
ein `OidcTokenProvider` (Authorization Code + PKCE) kommt in einer späteren Etappe dazu.

## Logging und Fehler

- Logs nach stdout, `text` (Entwicklung) oder `json` (Betrieb), jede Zeile mit
  Korrelations-ID: Request-ID (`X-Request-ID`, von nginx gesetzt oder generiert) bzw.
  `job-<id>` im Worker.
- Fehlerantworten im Format RFC 9457 (`application/problem+json`) mit `code` und
  `request_id`. Unerwartete Fehler liefern 500 ohne Details; der Stacktrace steht im Log.

## Deployment

- Ein Backend-Image für API (`musichub-serve`, migriert vor dem Start) und Worker
  (`musichub-worker`), ein Frontend-Image (nginx-unprivileged, statische Dateien + Proxy).
- Container laufen als Nicht-Root mit Read-only-Dateisystem.
- `compose.yaml` ohne YAML-Anker/`include`, damit Arcane sie für Updates selbst anpassen kann.
- CI: Lint/Tests → Image-Build → Smoke-Test des ganzen Stacks mit `compose.yaml` →
  Multi-Arch-Push nach GHCR → optional Arcane-Webhook.
