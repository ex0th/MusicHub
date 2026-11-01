# Prüfbericht Produktivbetrieb

Stand: 1. November 2026, Commit nach Migration `0007_anmeldung_sitzungen`.
Geprüft wurde der komplette Stand von Backend, Frontend, Compose-Dateien und Skripten.

**Legende:** ✅ automatisiert getestet · 🔁 zusätzlich in der CI gegen den echten
Docker-Stack · 🧪 manuell/End-to-End mit nachgebildeten Gegenstellen · ⚠️ nicht gegen das
echte Fremdsystem geprüft

## 1. Erfolgreich getestete Funktionen

| # | Bereich | Ergebnis | Nachweis |
|---|---|---|---|
| 1 | Backend-Tests | ✅ 395 bestanden, `ruff` sauber | `uv run pytest` gegen PostgreSQL 17 |
| 2 | Frontend-Tests | ✅ 33 bestanden | `pnpm test` (Vitest) |
| 3 | Produktions-Build | ✅ `astro check` 0 Fehler, statischer Build; 🔁 beide Images werden gebaut und gestartet | `pnpm build`, CI-Job „Images bauen, Stack testen“ |
| 4 | Datenbankmigrationen | ✅ `0001`–`0007` auf leerer DB, `0007` ↔ `0006` hin und zurück, `alembic check` ohne Abweichung; auch mit eingeschränkter App-Rolle | Testsuite migriert bei jedem Lauf; CI |
| 5 | Import/Export | ✅ 53 Tests (Formate, Analyse, Commit, Bericht, Export, Formel-Injection) 🔁 Import und Export über den Stack | `test_import_formats.py`, `test_import_export_api.py` |
| 6 | Matching | ✅ 119 Tests (Engine, Scan, Zusammenführen, Rückgängig) 🔁 Scan über den Stack | `test_matching*.py` |
| 7 | Plattform-Connectoren | ✅ 45 Tests gegen aufgezeichnete/nachgebildete API-Antworten (OAuth, Paging, Rate-Limits, Kontingent, nur GET beim Lesen) ⚠️ | `test_connectors.py`, `test_connections_api.py`, `test_connector_registry.py` |
| 8 | Synchronisation | ✅ 42 Tests (Freigaben, Dry-Run, Konflikte, Duplikate, fehlende Tracks, Rate-Limit, Schreibfehler ohne Wiederholung, YouTube-Kontingent) ⚠️ | `test_sync_engine.py`, `test_sync_api.py` |
| 9 | Authentifizierung | ✅ 60 Tests (Code Flow, PKCE, State, Nonce, Login-CSRF, Token-Prüfung, Gruppen, CSRF, Ablauf, Abmeldung, Rate-Limit, Open Redirect, Produktions-Validierung) 🧪 Browser-Durchlauf: Anmelden → Schreiben mit CSRF → Sitzung abgelaufen → erneut anmelden → Abmelden 🔁 Produktionsmodus mit OIDC startet, API ohne Sitzung `401`, gefälschtes Cookie `401`, Authentik nicht erreichbar → Fehlerseite ⚠️ | `test_auth_session.py`, `test_config_and_auth.py` |
| 10 | Fehlerbehandlung | ✅ 18 Sicherheits-/Robustheitstests: ungültige Eingaben `422` ohne interne Details, `500` ohne Details mit Request-ID, Uploads (Größe, Binärdaten, Dateiname), Header, CORS | `test_security.py` |
| 11 | Backup/Wiederherstellung | 🧪 lokal gegen PostgreSQL (Prüfsumme, Sicherheitskopie, Restore, App-Rolle) 🔁 Backup → Änderung → Restore → Stand wie vorher | `scripts/backup.sh`, `scripts/restore.sh` |
| 12 | Eigene DB-Rolle | 🧪 Rolle ohne Superuser, Besitz übertragen, idempotent; komplette Testsuite läuft mit dieser Rolle 🔁 Backend verbindet sich danach als `musichub_app` | `scripts/create-app-role.sh` |
| 13 | Container-Härtung | 🔁 keine Host-Ports für db/backend/worker, Healthchecks `healthy`, Worker-Heartbeat, read-only, `cap_drop` | CI-Smoke-Test |
| 14 | Compose-Dateien | ✅ `docker compose config` für Produktion, Override und Entwicklung | lokal und CI |

Während der Prüfung gefundene und behobene Fehler:

- Unbehandelte Fehler (`500`) lieferten `request_id: null` und keinen `X-Request-ID`-Header,
  weil der äußerste Fehler-Handler nach dem Zurücksetzen des Request-Kontexts läuft.
- Fehlermeldungen der Oberfläche verwiesen bei `401` auf „Token unter Einstellungen
  eingeben“ (nur im Dev-Modus richtig) und zeigten bei jedem `403` „Keine Schreibberechtigung“
  statt der tatsächlichen Ursache (z. B. CSRF).
- Ungenutzte Connector-Methoden aus Etappe 1 (`search_tracks`, `create_playlist`,
  `replace_playlist_tracks`, …) entfernt.

## 2. Mock-Daten, Platzhalter, unvollständige Implementierungen

- **Keine Mock-Daten im Produktivcode.** Nachbildungen gibt es nur in den Tests:
  `tests/fake_platforms.py` (Spotify/YouTube/SoundCloud-APIs) und `tests/fake_idp.py`
  (Authentik). `examples/import/` sind Beispieldateien für Formate und Tests.
- **Bewusst nicht implementiert, sichtbar gemeldet** (keine stillen Platzhalter):
  - SoundCloud als Sync-Ziel → API `422`, Oberfläche bietet es nicht an.
  - Operationen, die ein Connector nicht kann → `ConnectorNotImplementedError`
    („nicht unterstützt“), nicht wiederholbar.
  - OIDC ohne Client-Secret → Browser-Anmeldung meldet `not_configured`, nur Bearer-Tokens.
- **Nur gegen Nachbildungen getestet:** Authentik, Spotify, YouTube, SoundCloud, Nginx
  Proxy Manager. Die Nachbildungen folgen der jeweiligen Dokumentation; Abweichungen der
  echten Dienste sind möglich (siehe Abschnitt 5).

## 3. Noch offene Probleme und technische Schulden

| Thema | Auswirkung | Empfehlung |
|---|---|---|
| Lange Jobs gelten nach 15 min als hängend (`jobs/queue.py: STALE_AFTER`) und können von einem **zweiten** Worker erneut übernommen werden | nur bei mehreren Worker-Replikaten relevant | genau **einen** Worker betreiben (Standard); später Heartbeat je Job |
| Gruppenänderungen in Authentik wirken erst bei der nächsten Anmeldung | entzogenes Schreibrecht gilt bis zu 7 Tage weiter | Sitzungen per SQL löschen (SECURITY.md) oder `MUSICHUB_SESSION_MAX_HOURS` senken; später Refresh-Token-Prüfung |
| Geheimnisse als Umgebungsvariablen | sichtbar für Nutzer mit Docker-Zugriff | später Docker Secrets / `*_FILE`-Variablen |
| Rate-Limits im Backend je Prozess, Circuit-Breaker je Prozess | bei mehreren API-Replikaten schwächer | ein API-Container (Standard) |
| Import und Matching-Scan laufen synchron in der Anfrage | große Dateien (20 000 Zeilen) halten eine Anfrage einige Sekunden | als Job auslagern |
| Playlist-Detail lädt alle Einträge auf einmal | sehr große Playlists laden langsamer | Paging |
| Kein Audit-Log für Anmeldungen und Änderungen in der Datenbank | Nachvollziehbarkeit nur über Logs | Audit-Tabelle |
| Rücksprung nach Abmeldung bei Authentik | Authentik zeigt seine eigene Abmeldeseite, kein automatischer Rücksprung zu MusicHub | bei Bedarf `MUSICHUB_OIDC_LOGOUT_AT_PROVIDER=false` |
| `docker compose run … alembic downgrade` für Rollbacks | nur dokumentiert, nicht im Container getestet | beim ersten Update einmal auf einer Test-VM üben |
| Spotify-Ersetzen bei „vollständig abgleichen“ | ersetzt den Inhalt statt gezielt umzusortieren | Etappe 5b |

## 4. Bekannte API-Einschränkungen

**Spotify Web API** – App im *Development Mode*: höchstens 5 Nutzer, nur eigene und
kollaborative Playlists lesbar/schreibbar; App-Besitzer braucht Premium; Suche höchstens
10 Treffer je Anfrage; Schreiben je Aufruf höchstens 100 Einträge; Rate-Limits mit
`Retry-After`. Redirect-URI nur HTTPS oder `http://127.0.0.1`.

**YouTube Data API v3** – 10 000 Einheiten pro Tag; `playlistItems.insert/update/delete`
und `playlists.insert` je 50 Einheiten (≈ 200 Änderungen/Tag), `search.list` eigenes Limit
(100/Tag); OAuth-App im Status *Testing*: Anmeldung 7 Tage gültig, nur eingetragene
Testnutzer; Scope `youtube` für Schreiben.

**SoundCloud** – eigene API-App laut SoundCloud nur mit Artist Pro; ohne gültigen Zugang
nicht verifizierbar; kein Sync-Ziel.

**Authentik** – Signaturschlüssel am Provider Pflicht (sonst HS256); Gruppen kommen über
den Scope `profile`; Redirect-URI exakt. Getestet gegen eine Nachbildung nach der
offiziellen Dokumentation, nicht gegen eine echte Instanz.

Details: [CONNECTORS.md](CONNECTORS.md), [SYNC.md](SYNC.md).

## 5. Empfohlene nächste Schritte

1. **Testlauf auf der Ziel-VM** (nach Freigabe): Authentik-Anwendung anlegen, `.env`
   füllen, `docker compose up -d`, Checkliste in [DEPLOYMENT.md](DEPLOYMENT.md#7-prüfen)
   abarbeiten – das ist der erste Test gegen das echte Authentik und den echten NPM.
2. Eigene DB-Rolle einrichten (`scripts/create-app-role.sh`).
3. Tägliches Backup per Cron, Kopie außerhalb der VM, einmal Wiederherstellung üben.
4. `MUSICHUB_VERSION` auf einen festen Tag setzen und Updates bewusst einspielen, bis der
   Betrieb eingespielt ist; danach ggf. Arcane-Automatik.
5. Spotify/YouTube mit echten Zugangsdaten verbinden, zuerst nur Vorschau (Dry-Run),
   Schreiben erst nach Prüfung der Vorschau freigeben.
6. Danach Etappe 5b (Vorschläge auf der Sync-Seite bestätigen) und die Punkte aus Abschnitt 3.
