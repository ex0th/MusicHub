# Fehlersuche

Erste Schritte bei jedem Problem:

```bash
cd /opt/musichub
docker compose ps                                  # Status und Health
docker compose logs --since 15m backend worker     # JSON-Logs, Feld "correlation_id"
curl -s http://127.0.0.1:8080/api/health/ready     # Datenbank + Migrationen
```

Jede Fehlermeldung der Oberfläche und jede API-Antwort enthält eine **Request-ID**
(`request_id`, Header `X-Request-ID`). Damit findet man die passende Logzeile:
`docker compose logs backend | grep <request-id>`.

## Start und Container

| Symptom | Ursache | Lösung |
|---|---|---|
| `POSTGRES_PASSWORD in .env setzen` beim `up` | Passwort leer | `openssl rand -hex 24` in `.env` |
| Backend startet neu, Log: `Unsichere production-Konfiguration: …` | Pflichtwerte fehlen oder unsicher | genannte Variablen setzen (DEPLOYMENT.md, Abschnitt 4) |
| Log: `MUSICHUB_AUTH_MODE=dev ist in production nicht erlaubt` | Dev-Login in Produktion | `MUSICHUB_AUTH_MODE=oidc` |
| Log: `Für MUSICHUB_AUTH_MODE=oidc sind MUSICHUB_OIDC_ISSUER und MUSICHUB_OIDC_CLIENT_ID erforderlich` | OIDC-Werte fehlen | aus Authentik übernehmen |
| Backend `unhealthy`, Log `Can't locate revision …` | Datenbank ist neuer als das Image (Rollback) | BACKUP.md → „Rollback“, Fall 2 |
| `password authentication failed` | Passwort in `.env` geändert, Volume hat altes | altes Passwort zurück oder in psql `ALTER ROLE … PASSWORD` |
| Frontend startet nicht (`dependency failed to start`) | Backend nicht gesund | Backend-Log prüfen |
| Worker `unhealthy` | Worker hängt > 15 min oder wartet auf Migration | `docker compose logs worker`; bei sehr langen Sync-Läufen normal, danach wieder gesund |
| `no space left on device` | Logs/Backups | `KEEP_DAYS` kleiner, `docker system prune` (keine Volumes!) |
| `network npm_default declared as external, but could not be found` | Override-Datei mit falschem Netznamen | `docker network ls`, `MUSICHUB_PROXY_NETWORK` setzen |

## Anmeldung (Authentik)

Die Login-Seite (`/login/?error=…`) zeigt die Ursache. Im Backend-Log steht
`Anmeldung fehlgeschlagen: <code>`.

| Fehlerkennung | Ursache | Lösung |
|---|---|---|
| `not_configured` | Client-ID/-Secret oder `MUSICHUB_PUBLIC_URL` fehlen | `.env` ergänzen, `docker compose up -d` |
| `provider_unavailable` | Backend erreicht Authentik nicht (DNS, Zertifikat, Firewall) | `docker compose exec backend python -c "import httpx;print(httpx.get('https://auth.example.com/application/o/musichub/.well-known/openid-configuration').status_code)"` |
| Authentik zeigt „Redirect URI Error“ | Redirect-URI stimmt nicht exakt | in Authentik `https://<PUBLIC_URL>/api/auth/callback` (Strict) |
| `token_exchange_failed` | Client-Secret falsch, Client-Typ *Public*, Redirect-URI weicht ab | Werte in Authentik und `.env` vergleichen |
| `invalid_id_token` | kein **Signing Key** im Provider (HS256), Issuer mit/ohne `/` vertauscht, Uhrzeit falsch | Signing Key wählen; Issuer exakt aus Authentik kopieren; NTP prüfen |
| `invalid_state` | Login älter als 10 min, anderer Browser/Tab, Cookies blockiert, zweimal zurück geklickt | erneut anmelden; Cookies für die Domain erlauben |
| `forbidden` | Nutzer nicht in `MUSICHUB_OIDC_REQUIRED_GROUP` | Gruppe in Authentik zuweisen |
| `access_denied` | Zustimmung abgelehnt oder Policy-Bindung verweigert | Bindungen der Anwendung prüfen |
| Angemeldet, aber „Keine Schreibberechtigung“ | nicht in `MUSICHUB_OIDC_WRITE_GROUP` | Gruppe zuweisen, **neu anmelden** |
| Schreiben schlägt mit „CSRF-Token fehlt“ fehl | Seite aus altem Tab, Cookies gelöscht, Browser-Erweiterung blockiert Cookies | Seite neu laden bzw. neu anmelden |
| „Anfrage von fremder Herkunft abgelehnt“ | Aufruf über eine andere Adresse als `MUSICHUB_PUBLIC_URL` | immer über die öffentliche Adresse öffnen |
| Ständig „Sitzung abgelaufen“ | `MUSICHUB_PUBLIC_URL` ist `https`, Aufruf aber per `http` (Secure-Cookie wird verworfen) | NPM *Force SSL* aktivieren |
| Login-Schleife | Proxy entfernt `Set-Cookie` oder Domain falsch | NPM-Konfiguration ohne eigene Header-Manipulation |

## Reverse Proxy

| Symptom | Lösung |
|---|---|
| `502 Bad Gateway` von NPM | Ziel prüfen: Variante A Containername `musichub-frontend-1:8080` im selben Netz; B/C Port und Bind-Adresse |
| `413 Request Entity Too Large` beim Import | in NPM *Advanced*: `client_max_body_size 12m;` |
| Viele `429 Too Many Requests` | alle Clients erscheinen mit derselben Adresse → NPM-Adresse ist nicht privat; `set_real_ip_from` in `frontend/nginx/default.conf` ergänzen |

## Datenbank

```bash
docker compose exec db psql -U musichub -d musichub
select version_num from alembic_version;                 -- Schema-Stand
select status, count(*) from jobs group by 1;            -- Hintergrundjobs
select subject, display_name, last_seen_at from user_sessions;  -- angemeldete Sitzungen
```

Hängende Jobs (Worker war abgestürzt) werden nach Ablauf ihrer Sperre neu vergeben.

## Plattformen und Synchronisierung

Siehe [CONNECTORS.md](CONNECTORS.md) (Redirect-URIs, Kontingente, Development Mode) und
[SYNC.md](SYNC.md) (Freigaben, Konflikte). Häufig:

- „Kontingent der API erschöpft“ (YouTube): setzt um Mitternacht pazifischer Zeit zurück,
  der Lauf wartet und wird fortgesetzt.
- „Gespeichertes Token lässt sich nicht entschlüsseln“: `MUSICHUB_TOKEN_ENCRYPTION_KEY`
  wurde geändert → alten Schlüssel wieder ergänzen (kommagetrennt) oder Konto neu verbinden.
