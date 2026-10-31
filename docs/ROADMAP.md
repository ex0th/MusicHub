# Roadmap

Etappen bauen aufeinander auf. Jede Etappe endet mit grüner CI und einem lauffähigen Stand.

## ✅ Etappe 1 – Grundgerüst (dieser Stand)

- Projektstruktur, Backend (FastAPI) und Frontend (Astro) initialisiert
- PostgreSQL-Schema für Tracks, Plattform-Links, Playlists, Sync-Ziele, Jobs; Alembic
- Health-Checks (`live`, `ready` mit Migrationsprüfung)
- Logging (Text/JSON, Request-/Job-IDs), Fehlerformat RFC 9457
- Auth: Entwicklungs-Token, OIDC-Token-Validierung (JWKS), Schreibrechte per Gruppe
- Connector-Schnittstelle, Registry mit Fehlerisolation und Circuit Breaker
- Matching-Engine (ISRC + Fuzzy) und Sync-Planung als reine, getestete Funktionen
- Job-Queue in PostgreSQL mit Worker, Retry/Backoff
- Track-CRUD in API und Frontend
- Docker Compose (Dev mit Hot Reload, Betrieb für Arcane), CI mit Smoke-Test und GHCR

## Etappe 2 – Sammlung vollständig

- Playlists: CRUD, Einträge hinzufügen/entfernen/verschieben (API + UI)
- Eigene `artists`-Tabelle (n:m zu Tracks) statt Anzeige-String
- Import aus CSV/JSON (z. B. Exportdateien), Duplikaterkennung über Matching-Engine
- Volltextsuche (PostgreSQL `tsvector`/`pg_trgm`)

## Etappe 3 – Authentik im Frontend

- `OidcTokenProvider`: Authorization Code Flow + PKCE, Token-Erneuerung, Logout
- Konfiguration des Frontends zur Laufzeit (`/config.json` aus nginx)
- Benutzerbezug: Besitzer von Playlists/Connector-Konten

## Etappe 4 – Erster Connector: Spotify

- `connector_accounts`-Tabelle mit **verschlüsselten** OAuth-Tokens (Schlüssel aus Env)
- OAuth-Verbindung pro Benutzer, Token-Refresh
- Suche, ISRC-Lookup, Playlists lesen/anlegen/ersetzen
- Rate-Limits (HTTP 429, `Retry-After`) als wiederholbare Fehler
- Tests mit aufgezeichneten Antworten (kein Netz in der CI)

## Etappe 5 – Matching und Sync produktiv

- Job `matching.find_candidates`: Kandidaten je Track und Plattform, UI zum Bestätigen/Ablehnen
- Job `sync.playlist` pro Ziel und Plattform, Ausführung des `SyncPlan`
- Geplante Syncs (Zeitplan je Playlist), Anzeige von Sync-Historie und Fehlern
- Verfügbarkeitsprüfung bestehender Links (`unavailable`)

## Etappe 6 – SoundCloud und YouTube

- SoundCloud-Connector (OAuth, Playlists)
- YouTube-Connector (OAuth für Schreibzugriff, Quota-Verwaltung der Data API)

## Später / Ideen

- Import von Plattform-Playlists in die lokale Sammlung (Richtung Plattform → lokal)
- Metriken (Prometheus) und Job-Übersicht im Frontend
- Backup-Job (pg_dump in Volume/S3)
- API-Client-Typen aus OpenAPI generieren statt `types.ts` von Hand

## Offene Punkte / bekannte Grenzen

- Frontend-Anmeldung nur im Dev-Modus; mit `oidc` ist die API korrekt geschützt, die UI
  zeigt dann aber noch keinen Login → bis Etappe 3 Zugang per Reverse Proxy absichern.
- Connectoren melden Konfiguration, die Operationen sind noch nicht implementiert.
- Circuit-Breaker-Zustand ist pro Prozess (API und Worker getrennt), nicht geteilt.
- Keine Rate-Limits an der eigenen API (hinter Reverse Proxy ergänzen).
- Kein automatisiertes Backup.
