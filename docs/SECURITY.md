# Sicherheit

Stand der Prüfung: Etappe „Produktivbetrieb“ (Migration `0007`). Jede Maßnahme ist mit
dem Ort im Code und dem Test genannt, der sie absichert.

## Bedrohungsmodell in Kürze

MusicHub ist eine private Instanz hinter einem Reverse Proxy, erreichbar aus dem Internet.
Zu schützen sind die Bibliothek (Integrität), die Zugänge zu Spotify/YouTube (Tokens mit
Schreibrechten auf Playlists) und die Konfiguration (Client-Secrets). Angreifer sind
anonyme Internet-Nutzer, fremde Webseiten im Browser eines angemeldeten Nutzers (CSRF) und
angemeldete Nutzer ohne Schreibrecht.

## Anmeldung (Authentik, OIDC)

Authorization Code Flow mit PKCE als **vertraulicher Client** (Backend-for-Frontend): Das
Backend tauscht den Code, prüft das ID-Token und legt eine **serverseitige Sitzung** an.
Der Browser bekommt keine Tokens von Authentik zu sehen.

| Maßnahme | Umsetzung | Test |
|---|---|---|
| PKCE S256, `state`, `nonce` | `auth/oidc.py`, `api/routes/auth.py` | `test_login_redirects_to_provider_with_pkce`, `test_tampered_pkce_verifier_fails` |
| `state` einmalig, 10 min gültig, nur als SHA-256 gespeichert | `auth_logins` | `test_callback_state_is_single_use`, `test_callback_after_expiry` |
| Login-CSRF: Rückruf nur im Browser, der den Login gestartet hat (Bindungs-Cookie, daraus abgeleiteter PKCE-Verifier) | `SessionManager.consume_login` | `test_callback_from_other_browser_is_rejected` |
| ID-Token: Signatur (nur asymmetrisch, kein `none`/HS256), `iss`, `aud`, `exp`, `iat`, `nonce`, `azp` | `OidcProvider.verify_id_token` | `test_invalid_id_tokens_are_rejected`, `test_oidc_rejects_symmetric_and_none_algorithms` |
| Discovery-Issuer muss zur Konfiguration passen | `OidcProvider.discovery` | – |
| JWKS-Cache mit Neuladen bei unbekanntem `kid` (höchstens 1×/min) | `OidcProvider._key_set` | `test_oidc_rejects_foreign_signature` |
| Kein Open Redirect: Rückkehrpfad nur relativ, nicht `//`, nicht `/api/` | `safe_return_to` | `test_return_to_is_restricted_to_local_paths` |
| Pflichtgruppe (`MUSICHUB_OIDC_REQUIRED_GROUP`), Schreibgruppe (`…_WRITE_GROUP`) | `routes/auth.py`, `require_write` | `test_required_group_blocks_login`, `test_user_without_write_group_is_read_only` |
| Fehler beim Login → verständliche Fehlerseite, kein Stacktrace, Code-Tausch-Fehler ohne Body im Log | `/login/?error=…` | `test_provider_errors` |

**Bearer-Tokens** (für Skripte) bleiben im `oidc`-Modus möglich: JWT-Access-Tokens von
Authentik mit passender `aud`, geprüft wie oben. Sie brauchen keinen CSRF-Token, weil der
Browser sie nie automatisch mitsendet.

**Dev-Modus** (`MUSICHUB_AUTH_MODE=dev`, statisches Token) startet in `production` nicht.

## Sitzungen

- Token 256 Bit aus `secrets`, in der Datenbank nur SHA-256 (`user_sessions.token_hash`).
- Cookie `__Host-musichub_session`: `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, keine
  Domain (Präfix `__Host-` erzwingt das im Browser).
- Ablauf nach Inaktivität (`MUSICHUB_SESSION_IDLE_MINUTES`, Standard 12 h) und absolut
  (`MUSICHUB_SESSION_MAX_HOURS`, Standard 7 Tage). Abgelaufen → `401 session_expired`,
  Sitzung wird gelöscht, Cookies werden entfernt; die Oberfläche zeigt „Sitzung abgelaufen –
  erneut anmelden“ und kehrt danach auf dieselbe Seite zurück.
- Neue Anmeldung ersetzt eine vorhandene Sitzung (Schutz vor Session Fixation).
- Abmelden löscht die Sitzung serverseitig und leitet zur Abmeldung bei Authentik weiter
  (`MUSICHUB_OIDC_LOGOUT_AT_PROVIDER`).
- Der Worker löscht stündlich abgelaufene Sitzungen und Login-Vorgänge.
- Gruppen und Schreibrecht werden bei der Anmeldung übernommen. Änderungen in Authentik
  wirken ab der nächsten Anmeldung. Sofort sperren:
  `docker compose exec -T db psql -U musichub -d musichub -c "DELETE FROM user_sessions WHERE subject = '<sub>'"`
  (oder ohne `WHERE` für alle).

Tests: `test_idle_session_expires`, `test_absolute_session_lifetime`,
`test_activity_extends_idle_timeout`, `test_new_login_replaces_existing_session`,
`test_logout`, `test_purge_expired_sessions`.

## CSRF

- Jede verändernde Anfrage mit Sitzungs-Cookie braucht `X-CSRF-Token`. Der Wert ist ein
  HMAC des Sitzungs-Tokens; der Browser liest ihn aus dem Cookie `__Host-musichub_csrf`
  (`SameSite=Strict`, für JavaScript lesbar). Fremde Seiten können das Cookie weder lesen
  noch den Header setzen.
- Zusätzlich wird ein `Origin`-Header, der nicht zu `MUSICHUB_PUBLIC_URL` passt, abgelehnt.
- Geprüft in `current_principal` – damit für **jede** schreibende Route, weil alle über
  `require_write` davon abhängen (`test_every_write_route_requires_write_permission`).
  Einzige Ausnahme ohne Schreibrecht ist `POST /api/auth/logout`, die den CSRF-Token selbst prüft.
- `SameSite=Lax` am Sitzungs-Cookie verhindert zusätzlich, dass Cookies bei
  Cross-Site-POSTs mitgesendet werden.

Tests: `test_writes_require_csrf_token`, `test_foreign_origin_is_rejected`, `test_logout`.

## Keine ungeschützten Schreibzugriffe

- `/api/v1/*`: lesend `current_principal`, schreibend `require_write` – automatisch für alle
  Routen geprüft (`tests/test_api.py`).
- Plattformen: Schreiben nur über die Synchronisierung mit eigenen Freigaben ([SYNC.md](SYNC.md)).

## Geheimnisse

- Nur in `.env` (Rechte `600`, in `.gitignore`), nie im Repository. `.env.example` enthält
  keine Werte. Geprüft: kein Geheimnis in Dateien oder Git-Historie.
- In der Konfiguration als `SecretStr` – erscheinen nicht in Logs oder `repr`.
- Docker übergibt `.env` als Umgebungsvariablen. Wer Zugriff auf den Docker-Socket hat
  (`docker inspect`), sieht sie – Mitglieder der Gruppe `docker` sind faktisch root.
- GitHub Actions nutzt nur `GITHUB_TOKEN` und optional `ARCANE_WEBHOOK_URL` als Secret.

## Plattform-Tokens

- Fernet (AES-128-CBC + HMAC-SHA256) mit `MUSICHUB_TOKEN_ENCRYPTION_KEY`, Rotation über
  mehrere Schlüssel (`connections/crypto.py`). Ohne Schlüssel lassen sich keine Konten verbinden.
- Tokens verlassen den Server nie (API, Logs, Fehlermeldungen); Erneuerung in eigener
  Transaktion mit Zeilensperre.
- Nur notwendige Scopes, Schreib-Scopes nur auf ausdrücklichen Wunsch ([CONNECTORS.md](CONNECTORS.md)).

## CORS

Standard: aus. Frontend und API laufen auf demselben Origin. `MUSICHUB_CORS_ORIGINS` erlaubt
einzelne fremde Origins **ohne Credentials** (Cookies werden nie freigegeben); `*` ist in
`production` verboten. Test: `test_cors_is_closed_by_default`.

## Eingaben und Fehler

- Pydantic-Schemas mit Längen- und Wertebereichen, Enums und Mustern für Query-Parameter;
  ungültige Eingaben → `422` im Format RFC 9457 ohne Echo der Eingabe.
- Nur SQLAlchemy mit gebundenen Parametern, keine zusammengesetzten SQL-Strings aus Eingaben.
- Unbehandelte Fehler → `500 internal_error` mit Request-ID; Details nur im Log.
- CSV-Exporte mit Schutz vor Formel-Injection (`transfer/export.py::to_csv`).
- Dateinamen in `Content-Disposition` werden aus sicheren Bausteinen gebildet.

Tests: `tests/test_security.py` (überlange Felder, falsche Typen, SQL-Metazeichen,
ungültige UUIDs, unbekannte Formate, Fehler ohne interne Details, Header).

## Dateiuploads (Import)

- Größe: nginx `client_max_body_size 12m`, Backend `MUSICHUB_IMPORT_MAX_BYTES` (liest
  höchstens Grenze + 1 Byte), höchstens `MUSICHUB_IMPORT_MAX_ROWS` Datensätze.
- Inhalt wird nur als Text dekodiert und von Adaptern geparst – keine Ausführung, keine
  Archive, kein Speichern auf der Platte; der Dateiname ist nur Anzeige-Text.
- Format wird am Inhalt erkannt, nicht am Content-Type.
- Rate-Limit für Uploads im nginx (10/min je Client).

Tests: `test_upload_limits_and_binary_files`, `test_upload_filename_is_not_used_as_path`,
`test_import_formats.py`.

## Rate Limiting

| Wo | Grenze |
|---|---|
| nginx `/api/*` | 20 Anfragen/s je Client, Burst 80 |
| nginx `/api/auth/*` | 20/min je Client, Burst 20 |
| nginx `POST /api/v1/imports` | 10/min je Client, Burst 5 |
| Backend `/api/auth/login`, `/callback` | `MUSICHUB_AUTH_RATE_LIMIT_PER_MINUTE` (Standard 30) je Client |

Antwort `429` (Backend mit `Retry-After`). Die Client-Adresse übernimmt nginx nur aus
`X-Forwarded-For` von privaten Adressen (`set_real_ip_from`) und gibt dem Backend genau
diese eine Adresse weiter. Das Backend vertraut `X-Forwarded-For` von jedem Absender
(`MUSICHUB_FORWARDED_ALLOW_IPS=*`), weil es nur aus dem Frontend-Container erreichbar ist.
Test: `test_login_is_rate_limited`.

## HTTP-Header

nginx: `Content-Security-Policy` (`default-src 'self'`, kein Inline-Script, `frame-ancestors
'none'`, `form-action 'self'`), `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Permissions-Policy`. Backend zusätzlich für alle API-Antworten
`Cache-Control: no-store`. HSTS setzt Nginx Proxy Manager (Option *HSTS*).
Swagger-UI ist in `production` aus.

## Container

| | backend/worker | frontend | db |
|---|---|---|---|
| Benutzer | `musichub` (UID 10001) | `nginx` (UID 101) | `postgres` (nach Start) |
| Root-FS read-only | ja (`/tmp` als tmpfs, 64 MB) | ja | nein (Datenverzeichnis) |
| Capabilities | keine (`cap_drop: ALL`) | keine | nur CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID |
| `no-new-privileges` | ja | ja | ja |
| Prozesslimit | 256 | 128 | – |
| Host-Port | nein | nur 8080 (Standard `127.0.0.1`) | nein |
| Netz | internal + web | web | nur internal (ohne Internet) |
| Logs | rotiert (json-file, 10 MB × 3–5) | | |

## Datenbank

- Nur im internen Docker-Netz, kein Host-Port, Passwort-Authentifizierung mit SCRAM-SHA-256.
- Das offizielle Image legt `POSTGRES_USER` als **Superuser** an. Empfohlen ist eine eigene
  Rolle für Backend und Worker ohne Superuser-Rechte: `MUSICHUB_DB_USER`/`MUSICHUB_DB_PASSWORD`
  in `.env` setzen, `scripts/create-app-role.sh` ausführen, `docker compose up -d`. Die Rolle
  besitzt Datenbank, Schema und Tabellen (für Migrationen nötig), kann aber keine anderen
  Datenbanken, Rollen oder Server-Dateien anfassen. Die CI prüft das gegen den echten Stack;
  die komplette Testsuite läuft auch mit dieser Rolle.
- Backups enthalten keine Rollen/Passwörter (`--no-owner`).

## Bekannte Grenzen

- Rate-Limits im Backend gelten je Prozess (ein API-Prozess je Container).
- Gruppenänderungen in Authentik wirken erst bei der nächsten Anmeldung (siehe oben).
- Keine Zwei-Faktor-Logik in MusicHub selbst – über die Flows in Authentik konfigurieren.
- Geheimnisse als Umgebungsvariablen statt Docker Secrets.
- Keine Audit-Log-Tabelle für Anmeldungen; Anmeldungen stehen im Backend-Log
  (`Anmeldung erfolgreich` mit `subject`).
