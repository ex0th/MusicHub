# Architektur

## Leitprinzipien

1. **Die lokale Datenbank ist die führende Instanz.** Tracks, Playlists und ihre
   Reihenfolge leben in PostgreSQL. Plattformen sind Ziele (und später Importquellen),
   nie die Wahrheit.
2. **Ein Track braucht keine Plattform.** Plattform-Zuordnungen hängen als
   `TrackSource` am Track. Ein Track ohne Quellen ist ein vollwertiger Track.
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
| 2a | Bibliothek | `musichub.library` | Fachlogik: Tracks, Quellen, Tags, Playlists, Statistik, Matching-Vorschläge, Zusammenführen/Rückgängig | `models`, `matching`, `domain`, `core` |
| 2b | Import/Export | `musichub.transfer` | Dateiprüfung, Format-Adapter, Abgleich, Importlauf, Export | `library`, `matching`, `models`, `domain` |
| 3 | Datenbank | `musichub.models`, `musichub.db`, `musichub/migrations` | Schema, Sessions, Migrationen | `domain` |
| 4 | Connectoren | `musichub.connectors` | Plattform-APIs, liefern neutrale DTOs | `domain`, `core` – **nicht** `db` |
| 5 | Matching-Engine | `musichub.matching` | Gleiche Aufnahme/Version erkennen, Kandidatensuche (rein) | `domain` |
| 6 | Synchronisation | `musichub.sync` | Planung (rein), Ziele, Ausführung als Job | `engine`: `domain`; `service`/`executor`: `models`, `connections`, `matching` |
| 7 | Hintergrundjobs | `musichub.jobs` | Queue, Worker, Job-Handler | alle außer `api` |

Querschnitt: `musichub.core` (Konfiguration, Logging, Fehler, Middleware),
`musichub.auth`, `musichub.domain` (gemeinsame Aufzählungstypen).

Abhängigkeitsrichtung: `api`/`jobs` → `library`/`matching`/`sync`/`connectors` → `models` → `domain`/`core`.
Routen sind dünn: Sie validieren, rufen eine Funktion aus `musichub.library` auf und
committen. Bibliotheksfunktionen committen nie selbst (eine Anfrage = eine Transaktion).
Connectoren, Matching und Sync kennen sich gegenseitig nicht; erst Jobs/Services
verbinden sie.

## Connectoren und Fehlerisolation

Jeder Connector implementiert `PlatformConnector` (`connectors/base.py`:
`spotify.py`, `youtube.py`, `soundcloud.py`) und liefert neutrale DTOs (`RemoteTrack`,
`RemotePlaylistInfo`, `TrackPage`, `RemoteAccount`, `OAuthTokens`). Connectoren speichern
keine Tokens und kennen keine Datenbank – Tokens kommen bei jedem Aufruf aus
`connections/service.py` (entschlüsselt, bei Bedarf erneuert). Der gemeinsame HTTP-Client
(`connectors/http.py`) lässt gegenüber den Plattform-APIs nur GET zu (Etappe lesend),
behandelt `429`/`Retry-After` und Kontingentfehler. Details: [CONNECTORS.md](CONNECTORS.md).
Aufrufe laufen **ausschließlich** über `ConnectorRegistry.call(platform, fn)`:

- Jede Ausnahme wird zu `ConnectorError` mit `platform` und `retryable`.
  Fremde Ausnahmen (`httpx`, `KeyError` aus kaputtem JSON …) werden eingekapselt.
- **Circuit Breaker pro Plattform**: nach 3 wiederholbaren Fehlern in Folge wird die
  Plattform 60 s nicht angesprochen (`ConnectorUnavailableError`). Andere Plattformen
  laufen weiter.
- `ConnectorNotConfiguredError` / `ConnectorNotImplementedError` sind nicht wiederholbar
  und zählen nicht als Störung. `ConnectorAuthError` (Anmeldung ungültig) und
  `ConnectorAccessError` (403/404) sind nicht wiederholbar; `RateLimitedError` trägt
  `retry_after`, das der Worker als Wartezeit bis zum nächsten Versuch übernimmt.
- Status je Plattform: `not_configured`, `ready`, `degraded`, `suspended`
  (`GET /api/v1/connectors`).

Der Breaker-Zustand liegt im Speicher des jeweiligen Prozesses (API und Worker getrennt).
Plattformübergreifende Vorgänge (z. B. "Playlist auf alle Ziele synchronisieren") werden
als **ein Job pro Plattform** eingestellt, sodass ein Fehlschlag nur diesen Job betrifft.

## Plattform-Konten

`musichub.connections`: `crypto.py` (Fernet-Verschlüsselung der Tokens, Schlüsselrotation),
`service.py` (OAuth-Start und -Rückruf mit einmaligem `state`, Token-Erneuerung in eigener
Transaktion, Trennen, Status), `importer.py` (Jobs `platform.refresh_playlists`,
`platform.import_playlist`, `platform.refresh_metadata`). Der Import nutzt denselben
Importlauf wie Dateien (`transfer/service.analyze_parsed` + `commit`), erscheint also im
Importprotokoll und erkennt Vorhandenes gleich. Netzwerkzugriffe auf Plattformen laufen nur
im Worker; die API plant Jobs ein (Ausnahme: Code-Tausch im OAuth-Rückruf).

## Bibliothek

- **Verfügbarkeit** ist überall gleich definiert (`library/queries.py`): bestätigte Quelle
  (`link_status = linked`) und nicht `availability = unavailable`. Filter, Statistik und
  der Abdeckungsstreifen im Frontend nutzen dieselbe Regel.
- **Plattform-Links** erkennt `domain/platform_urls.py` ohne Netzwerk; daraus entstehen
  kanonische URLs und geprüfte IDs. So ist die Bibliothek ohne Connectoren voll nutzbar.
- **Playlists**: Jede Änderung sperrt die Playlist-Zeile (`FOR UPDATE`) und verschiebt
  Positionen mit einem `UPDATE`; die Eindeutigkeit wird erst beim Commit geprüft.
  Positionen bleiben lückenlos `0..n-1`.
- **Löschen**: Tracks in Playlists nur mit `force`, dann werden betroffene Playlists neu
  durchnummeriert. Tags und Playlists zu löschen lässt Tracks unberührt.

## Import und Export

Details in [IMPORT_EXPORT.md](IMPORT_EXPORT.md). Aufbau von `musichub.transfer`:

```
Upload ─► decoding (Typ, Größe, Kodierung, Binärinhalt)
       ─► formats  (ein Adapter je Format ─► ImportRecord/ParseResult)
       ─► matching (LibraryIndex: ID, Quelle, ISRC, Schlüssel; Ähnlichkeit) ─ rein
       ─► service.analyze  ─► import_runs/import_items  (Vorschau, Entscheidungen)
       ─► service.commit   ─► Tracks/Quellen/Tags/Playlists (1 Transaktion, Savepoint je Datensatz)
export ─► MusicHub-CSV/-JSON (Formula-Injection-Schutz) ─► wieder importierbar
```

Adapter kennen weder Datenbank noch Bibliothek; sie übersetzen nur Dateien in das neutrale
`ImportRecord`. Formate, deren Aufbau nicht belegt ist, werden nicht geraten – sie laufen
über den allgemeinen CSV-/JSON-Adapter mit dokumentierten Spaltennamen.

## Matching

Details, Score-Tabelle und Regeln: [MATCHING.md](MATCHING.md).

```
matching/engine.py   parse() + compare() → Confidence 0–100, Stufe, Gründe, Blocker   (rein)
matching/blocking.py Kandidatenpaare über ISRC / Titel / Artist+Titelanfang          (rein)
library/matching.py  Scan → match_candidates, Entscheidungen, automatische Zusammenführung
library/merge.py     Zusammenführen + Rückgängig über Snapshot in track_merges
```

Stufen: `certain` (≥ 95 und harter Beleg, ohne Blocker – nur diese dürfen automatisch
zusammengeführt werden), `likely` (85–94), `review` (60–84), `different` (< 60). Abweichende
Versionen setzen eine Obergrenze, die kein Bonus aufhebt. Der Import nutzt dieselbe Engine
für „mögliche Duplikate“ und plant nach dem Commit den Job `matching.scan` ein.

## Synchronisation

Richtung: **MusicHub → Plattform** (Details, Freigaben und Modi: [SYNC.md](SYNC.md)).

```
sync/engine.py    plan_sync(): add_only | mirror, externe Änderungen, Duplikate,
                  reorder_moves()                                          (rein)
sync/service.py   Ziele, Schreib-Freigaben, Vorschau/Sync anfordern, Zeitplan, Historie
sync/executor.py  Job sync.run: Playlist lesen → Tracks bestimmen (Links, Suche) →
                  Ziel lesen → planen → Vorschau | ausführen → prüfen → dokumentieren
```

Schreiben nur, wenn Plattform in `MUSICHUB_SYNC_WRITE_PLATFORMS`, Konto mit Schreib-Scopes,
Ziel aktiviert und erste Vorschau bestätigt. Schreibaufrufe (`ApiClient.write`) werden nie
automatisch wiederholt; nur Rate-Limits führen zu einem zeitversetzten Neuversuch, der neu
plant (idempotent). Der Worker plant fällige Ziele (`enqueue_due`) selbst ein.

## Hintergrundjobs

- Tabelle `jobs`, abgeholt mit `SELECT … FOR UPDATE SKIP LOCKED` → mehrere Worker möglich.
- Status: `queued` → `running` → `succeeded` | `failed` (Retry mit Backoff 30 s · 2ⁿ, max. 1 h)
  | `dead` (endgültig oder `max_attempts` erreicht).
- Hängende Jobs (`running` > 15 min) werden neu vergeben.
- Worker beendet sich bei SIGTERM nach dem aktuellen Job (`stop_grace_period: 60s`).
- Neuer Job-Typ: Funktion in `jobs/handlers.py` mit `@handler("bereich.aktion")`.

## Frontend

Statisches Astro-Projekt ohne UI-Framework. Seiten (`src/pages/*/index.astro`) liefern das
Gerüst, Seiten-Skripte bauen Inhalte mit dem kleinen DOM-Helfer `lib/ui.ts::h()` (nur
`textContent`, kein `innerHTML` mit Nutzerdaten). Detailseiten nutzen Query-Parameter
(`/track/?id=…`, `/playlist/?id=…`), weil der Build statisch ist. Filter der Bibliothek
stehen in der URL und überleben Zurück/Neu laden.

Gestaltung (`src/styles/global.css`): dunkles Schema als Standard (hell/System per
Einstellung, gesetzt durch `public/theme.js` vor dem ersten Rendern), Akzent `#FF8000` nur
für Handlungen, aktive Navigation und Fokus. Wiederkehrendes Element ist der
**Abdeckungsstreifen**: drei feste Felder Spotify/SoundCloud/YouTube – gefüllt =
verfügbar (Link zur Plattform), gestrichelt = nicht verknüpft, rot durchgestrichen =
nicht mehr verfügbar. Seitenleiste ab 900 px, darunter Tab-Leiste unten.

## Authentifizierung

Backend-for-Frontend: Der Browser meldet sich über das Backend bei Authentik an
(Authorization Code Flow mit PKCE, vertraulicher Client) und erhält nur ein
Sitzungs-Cookie; Tokens von Authentik verlassen den Server nicht.

```
auth/oidc.py          OidcProvider: Discovery, JWKS (Cache, Key-Rotation), Code-Tausch,
                      ID-Token- und Access-Token-Prüfung (nur asymmetrische Signaturen)
auth/sessions.py      SessionManager: Login-Zustand (auth_logins), Sitzungen
                      (user_sessions), Cookies, CSRF, Ablauf, Aufräumen
auth/verifiers.py     Bearer: DevTokenVerifier (dev), OidcVerifier (Access-Tokens)
auth/dependencies.py  current_principal (Bearer oder Cookie + CSRF), require_write
api/routes/auth.py    /api/auth/session, /login, /callback, /logout
```

Endpunkte hängen nur von `current_principal` (lesen) bzw. `require_write` (schreiben) ab;
CSRF wird dort für alle verändernden Methoden geprüft. Ein Test (`tests/test_api.py`)
prüft für **alle** Routen, dass schreibende Methoden `require_write` und alle
`/api/v1`-Routen eine Anmeldung verlangen. Netzwerkzugriffe auf Authentik laufen über
einen injizierbaren `httpx`-Transport (`create_app(oidc_transport=…)`, in Tests
`tests/fake_idp.py`).

Frontend: `lib/auth.ts` (`BrowserAuth`: dev-Token oder CSRF-Cookie), `lib/session.ts`
(`requireLogin`, Hinweis „Sitzung abgelaufen“, `logout`), Seite `/login/` für Fehler und
Abmeldung. Sicherheitsmerkmale: [SECURITY.md](SECURITY.md).

## Logging und Fehler

- Logs nach stdout, `text` (Entwicklung) oder `json` (Betrieb), jede Zeile mit
  Korrelations-ID: Request-ID (`X-Request-ID`, von nginx gesetzt oder generiert) bzw.
  `job-<id>` im Worker.
- Fehlerantworten im Format RFC 9457 (`application/problem+json`) mit `code` und
  `request_id`. Unerwartete Fehler liefern 500 ohne Details; der Stacktrace steht im Log.

## Deployment

- Ein Backend-Image für API (`musichub-serve`, migriert vor dem Start) und Worker
  (`musichub-worker`), ein Frontend-Image (nginx-unprivileged, statische Dateien + Proxy).
- Container laufen als Nicht-Root mit Read-only-Dateisystem, ohne Capabilities; die
  Datenbank nur im internen Netz. Betrieb: [DEPLOYMENT.md](DEPLOYMENT.md),
  [BACKUP.md](BACKUP.md), [SECURITY.md](SECURITY.md).
- `compose.yaml` ohne YAML-Anker/`include`, damit Arcane sie für Updates selbst anpassen kann.
- CI: Lint/Tests → Image-Build → Smoke-Test des ganzen Stacks mit `compose.yaml` →
  Multi-Arch-Push nach GHCR → optional Arcane-Webhook.
