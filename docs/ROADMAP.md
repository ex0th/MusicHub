# Roadmap

Etappen bauen aufeinander auf. Jede Etappe endet mit grüner CI und einem lauffähigen Stand.

## ✅ Etappe 1 – Grundgerüst

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

## ✅ Etappe 2 – Zentrale Musikbibliothek

- Datenmodell: Tracks mit Version/Remix und Veröffentlichungsdatum, `track_sources` mit
  getrenntem Verknüpfungsstatus und Verfügbarkeit, Tags mit Kategorien, lückenlose
  Playlist-Positionen (Migration `0002` übernimmt Bestandsdaten)
- REST-API: Tracks (Suche, Filter nach Plattform/Tags/Anzahl, Sortierung, Pagination),
  Quellen per Link oder ID, Tags, Playlists inkl. Einfügen an Position, Verschieben,
  komplette Neuordnung, Statistik
- Plattform-Links werden ohne Netzwerk erkannt (Spotify, YouTube, SoundCloud)
- Frontend: Dashboard, Musikbibliothek, Track-Detail, Playlists mit Drag & Drop,
  Plattformen, Einstellungen (Anmeldung, Farbschema, Tags); dunkel als Standard
- Vollständig ohne externe APIs nutzbar

## ✅ Etappe 2a – Import und Export

- Import-Assistent: hochladen, Format erkennen, Vorschau, Abgleich, Entscheidungen, bestätigen
- Adapter: TuneMyMusic-, Soundiiz-, Exportify- und allgemeines CSV, JSON, TXT-Tracklisten,
  MusicHub-CSV/-JSON; UTF-8/UTF-16/Windows-1252, Trennzeichen `,` `;` Tab `|`
- Erkennung vorhandener Tracks über MusicHub-ID, Plattform-Link, ISRC, Titel+Artist+Version
  und Ähnlichkeit; wiederholte Imports erzeugen keine Duplikate
- Importprotokoll (`import_runs`, `import_items`), Bericht als CSV/JSON
- Export der Bibliothek und einzelner Playlists als CSV/JSON, gegen Formula-Injection gesichert

## ✅ Etappe 2c – Track-Matching

- Mehrstufige Engine: Normalisierung, Versionserkennung (Remix, VIP, Bootleg, Edit, Extended,
  Live, Cover …) mit Bearbeiter, Featuring, „Artist - Titel“-Lesart, ISRC, Dauer,
  Veröffentlichung, Plattform-Metadaten; Confidence 0–100 mit Gründen, vier Stufen
- Konservative Automatik: nur „sicher“ mit hartem Beleg und ohne Blocker, vorher neu geprüft
- Vorschläge (`match_candidates`), Ablehnungen werden gemerkt; Scan per API und als Job nach
  jedem Import
- Zusammenführen ohne Datenverlust (Quellen, Tags, Playlist-Positionen, Importprotokoll) und
  vollständig rückgängig (`track_merges`)
- Bereich „Matching“: Gegenüberstellung, Gründe, Bestätigen/Ablehnen/Später prüfen, Verlauf,
  manueller Vergleich

## ✅ Etappe 4a – Plattform-Connectoren, lesend

- Spotify Web API (Development-Mode-Regeln 2026 berücksichtigt), YouTube Data API v3,
  SoundCloud vorbereitet (Artist Pro nötig, kein Umgehungsweg) – Details in CONNECTORS.md
- OAuth mit einmaligem `state` und PKCE (YouTube, SoundCloud), Tokens nur verschlüsselt
  (`connector_accounts`), minimale Lese-Scopes, Trennen mit Widerruf (YouTube)
- Hintergrundjobs mit Fortschritt, Ergebnis, Fehlerprotokoll, Rate-Limit-/Kontingent-
  Behandlung (`Retry-After`), Backoff und inkrementeller Aktualisierung (Versionsmerkmal)
- Import über den normalen Importlauf (Importprotokoll, Duplikaterkennung, nur ergänzen);
  Metadaten-/Verfügbarkeitsabgleich verknüpfter Tracks
- Seite „Plattformen“: Status, Konto, letzter Import, importierbare Playlists, Jobs
- Streng lesend: keine Änderung an Plattform-Playlists (HTTP-Client nur GET, getestet)

## ✅ Etappe 5a – Synchronisierung MusicHub → Plattform (dieser Stand)

- Ziele je Playlist (mehrere, auch je Plattform), Modi „nur hinzufügen“ und „vollständig
  abgleichen“ (nur verwaltete, freigegebene Ziele), manuell und geplant
- Freigaben: `MUSICHUB_SYNC_WRITE_PLATFORMS`, Schreib-Scopes beim Verbinden, bestätigte
  Vorschau vor dem ersten Schreiben, Zustimmung bei externen Änderungen
- Dry-Run, Konflikterkennung über bekannte Einträge, Duplikatvermeidung, idempotente Läufe
- Fehlende Tracks: Suche (Spotify ISRC/Titel, YouTube) mit Matching-Engine, Status
  ungeprüft/nicht gefunden/API-Fehler/Vorschlag/unverfügbar, erneute Prüfung
- Historie (`sync_runs`), Seite „Synchronisation“ je Playlist
- Spotify und YouTube schreibend; SoundCloud bewusst nicht

## Etappe 2b – Bibliothek ausbauen

- Eigene `artists`-Tabelle (n:m zu Tracks) statt Anzeige-String
- Import großer Dateien als Hintergrundjob mit Fortschrittsanzeige
- Weitere Formate mit belegtem Aufbau (z. B. M3U/XSPF), echte Soundiiz-Exportdatei prüfen
- Mehrfachauswahl in der Bibliothek (Tags setzen, zu Playlist hinzufügen, löschen)
- Fehlertolerante Suche (`pg_trgm`) für große Sammlungen
- Paginierte Einträge für sehr große Playlists (> 2 000 Tracks)

## Etappe 3 – Authentik im Frontend

- `OidcTokenProvider`: Authorization Code Flow + PKCE, Token-Erneuerung, Logout
- Konfiguration des Frontends zur Laufzeit (`/config.json` aus nginx)
- Benutzerbezug: Besitzer von Playlists/Connector-Konten

## Etappe 4b – Konten und Plattformen ausbauen

- SoundCloud-Metadatenabgleich über `/resolve`, „Lieblingssongs“ (Spotify) optional
- Konten pro Benutzer (mit Etappe 3), mehrere Konten je Plattform
- SoundCloud als Sync-Ziel, sobald ein zulässiger API-Zugang geprüft werden kann

## Etappe 5b – Sync ausbauen

- Vorschläge (`candidate`) aus der Suche direkt auf der Sync-Seite bestätigen
- Spotify-Ersetzen gezielter (`snapshot_id`, Reihenfolge per Reorder statt Ersetzen)
- Benachrichtigung bei Konflikten geplanter Läufe

## Etappe 6 – SoundCloud

- SoundCloud-Connector mit echten Zugangsdaten verifizieren (bisher nur nachgebildet getestet)

## Später / Ideen

- Import von Plattform-Playlists in die lokale Sammlung (Richtung Plattform → lokal)
- Metriken (Prometheus) und Job-Übersicht im Frontend
- Backup-Job (pg_dump in Volume/S3)
- API-Client-Typen aus OpenAPI generieren statt `types.ts` von Hand

## Offene Punkte / bekannte Grenzen

- Frontend-Anmeldung nur im Dev-Modus; mit `oidc` ist die API korrekt geschützt, die UI
  zeigt dann aber noch keinen Login → bis Etappe 3 Zugang per Reverse Proxy absichern.
- Schreiben nur für Playlist-Inhalte über die Synchronisierung; Importe lesen nur.
- YouTube-Sync verbraucht 50 Einheiten je Änderung (≈ 200 Änderungen pro Tag); große
  Playlists brauchen mehrere Tage, der Lauf setzt nach dem Zurücksetzen fort.
- Plattform-Suche nur für Spotify und YouTube, höchstens `MUSICHUB_SYNC_LOOKUP_LIMIT` je Lauf.
- Ein Konto je Plattform und Instanz; Spotify im Development Mode max. 5 Nutzer und nur
  eigene/kollaborative Playlists; YouTube im Status „Testing“ Anmeldung 7 Tage gültig.
- SoundCloud nur mit eigener API-App (Artist Pro) und bisher nur gegen nachgebildete
  Antworten getestet.
- Auf der Plattform entfernte Tracks bleiben in importierten Playlists (nur ergänzen).
- Pro Track und Plattform ist genau eine bestätigte Quelle möglich (z. B. nicht
  gleichzeitig Musikvideo und Audio auf YouTube); weitere nur als Vorschlag.
- Playlist-Detail lädt alle Einträge auf einmal.
- Circuit-Breaker-Zustand ist pro Prozess (API und Worker getrennt), nicht geteilt.
- Keine Rate-Limits an der eigenen API (hinter Reverse Proxy ergänzen).
- Kein automatisiertes Backup (der JSON-Export eignet sich als manuelles Backup der Bibliothek).
- Import läuft synchron in der Anfrage (bis 20 000 Datensätze); Soundiiz-Exportlayout nur
  aus der Soundiiz-Hilfe abgeleitet, TuneMyMusic aus Drittquellen (siehe IMPORT_EXPORT.md).
- SoundCloud-IDs aus Fremdexporten (numerisch) werden noch nicht übernommen.
- Matching-Scan läuft synchron in der Anfrage (≈ 2 s je 10 000 Tracks); nach Importen
  zusätzlich als Job. Ketten (A = B, B = C) werden schrittweise über erneute Scans erkannt.
- Rückgängig ist nur in umgekehrter Reihenfolge möglich, wenn der behaltene Track später
  selbst zusammengeführt wurde.
