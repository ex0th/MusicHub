# Deployment – Produktivbetrieb auf einer Linux-VM

Zielbild: MusicHub läuft mit Docker Compose auf einer eigenen Linux-VM. **Nginx Proxy
Manager** (NPM) terminiert HTTPS, **Authentik** übernimmt die Anmeldung (OpenID Connect).
Von außen ist nur NPM erreichbar; Datenbank und Backend haben keine offenen Ports.

```
Browser ──HTTPS──> Nginx Proxy Manager ──HTTP──> frontend (nginx, :8080)
                                                   ├─ statische Seiten
                                                   └─ /api ──> backend (:8000) ──> db (PostgreSQL)
                                                                worker ───────────┘
backend ──HTTPS──> Authentik (Discovery, Token, JWKS)      worker ──> Spotify/YouTube
```

| Netz | Dienste | Zweck |
|---|---|---|
| `internal` (Docker, `internal: true`) | db, backend, worker | Datenbank – kein Internet, kein Host-Port |
| `web` | frontend, backend, worker | Ausgang ins Internet (Authentik, Plattform-APIs) |
| optional `proxy` (extern) | frontend | gemeinsames Netz mit NPM (`compose.override.example.yaml`) |

Begleitdokumente: [SECURITY.md](SECURITY.md) · [BACKUP.md](BACKUP.md) ·
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) · Plattform-Zugänge: [CONNECTORS.md](CONNECTORS.md)

---

## 1. Voraussetzungen

- Linux-VM (Debian 12/13 oder Ubuntu 24.04), 2 vCPU, 2 GB RAM, 10 GB Platte reichen für
  eine private Sammlung.
- Docker Engine ≥ 25 mit Compose-Plugin ≥ 2.24 (`docker compose version`).
  `start_interval` in Healthchecks und `!reset` in der Override-Datei setzen das voraus.
- DNS-Eintrag, z. B. `music.example.com`, zeigt auf den Nginx Proxy Manager.
- Erreichbares Authentik mit HTTPS, z. B. `https://auth.example.com`.
- Uhrzeit per NTP synchron (Token-Prüfung erlaubt 30 s Abweichung).

## 2. Authentik einrichten

1. **Gruppen anlegen** (*Directory → Groups*):
   `musichub-admins` (darf schreiben) und optional `musichub-users` (nur lesen).
2. **Anwendung mit Provider anlegen** (*Applications → Applications → Create with provider*):
   - Name `MusicHub`, **Slug `musichub`**
   - Provider-Typ **OAuth2/OpenID Provider**
   - Authorization flow: `default-provider-authorization-implicit-consent` (oder explizit)
   - **Client type: Confidential**
   - **Redirect URI** (Modus *Strict*, keine Regex): `https://music.example.com/api/auth/callback`
   - **Signing Key: ein Zertifikat auswählen** (z. B. *authentik Self-signed Certificate*).
     Ohne Signing Key signiert Authentik mit HS256 und dem Client-Secret – MusicHub lehnt
     solche Tokens bewusst ab (Anmeldefehler `invalid_id_token`).
   - Scopes: Standard (`openid`, `email`, `profile`). Die Gruppen liefert Authentik im
     Scope `profile` als Claim `groups`.
   - Subject mode: Standard (*Based on the User's hashed ID*) ist in Ordnung.
3. **Zugriff beschränken** (*Application → Policy / Group / User Bindings*): Gruppen
   `musichub-admins` und `musichub-users` binden. Ohne Bindung darf sich jeder
   Authentik-Nutzer anmelden (nur lesen, solange er nicht in `musichub-admins` ist).
   Zusätzlich kann `MUSICHUB_OIDC_REQUIRED_GROUP` gesetzt werden.
4. Notieren: **Client ID**, **Client Secret** und den **OpenID Configuration Issuer**
   (`https://auth.example.com/application/o/musichub/`).

## 3. Server vorbereiten

```bash
sudo mkdir -p /opt/musichub && sudo chown "$USER" /opt/musichub && cd /opt/musichub
git clone https://github.com/ex0th/MusicHub.git .      # oder nur compose.yaml, .env.example, scripts/
cp .env.example .env && chmod 600 .env
```

Firewall (Beispiel `ufw`, nur SSH und – falls NPM auf dieser VM läuft – 80/443):

```bash
sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
```

> Docker veröffentlicht Ports an `ufw` vorbei. Deshalb bindet MusicHub den Frontend-Port
> standardmäßig nur an `127.0.0.1` – siehe Abschnitt 5.

## 4. Konfiguration (`.env`)

Geheimnisse erzeugen:

```bash
openssl rand -hex 24                       # POSTGRES_PASSWORD
openssl rand -hex 24                       # MUSICHUB_DB_PASSWORD (eigene App-Rolle, empfohlen)
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
                                           # MUSICHUB_TOKEN_ENCRYPTION_KEY
```

Mindestens setzen:

| Variable | Wert |
|---|---|
| `MUSICHUB_APP_ENV` | `production` |
| `MUSICHUB_PUBLIC_URL` | `https://music.example.com` (ohne `/` am Ende, ohne Pfad) |
| `POSTGRES_PASSWORD` | Zufallswert (nur Buchstaben/Ziffern) |
| `MUSICHUB_AUTH_MODE` | `oidc` |
| `MUSICHUB_OIDC_ISSUER` | `https://auth.example.com/application/o/musichub/` |
| `MUSICHUB_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | aus Authentik |
| `MUSICHUB_OIDC_WRITE_GROUP` | `musichub-admins` |
| `MUSICHUB_TOKEN_ENCRYPTION_KEY` | nur für Plattform-Konten nötig, dann **sichern** (BACKUP.md) |

Der Backend-Start **verweigert** in `production` eine unsichere Konfiguration: Dev-Login,
fehlende Client-Daten, `http://` als öffentliche Adresse, `MUSICHUB_SESSION_COOKIE_SECURE=false`
oder CORS `*`. Die Fehlermeldung steht im Log (`docker compose logs backend`).

Alle Variablen mit Erklärung: [`.env.example`](../.env.example).

## 5. Anbindung an Nginx Proxy Manager

**Variante A – gemeinsames Docker-Netz (empfohlen, wenn NPM auf derselben VM läuft):**

```bash
docker network ls                                   # Netz von NPM, z. B. npm_default
cp compose.override.example.yaml compose.override.yaml
echo "MUSICHUB_PROXY_NETWORK=npm_default" >> .env
```

Der Frontend-Port wird dann gar nicht auf dem Host veröffentlicht. Ziel in NPM:
`http://musichub-frontend-1:8080`.

**Variante B – NPM auf einem anderen Host:** `MUSICHUB_HTTP_BIND` auf die LAN-Adresse
der VM setzen und den Port in der Firewall **nur für die IP des NPM-Hosts** öffnen.
Ziel in NPM: `http://<vm-ip>:8080`.

**Variante C – NPM mit `network_mode: host`:** Standard `127.0.0.1:8080` passt.

Proxy Host in NPM:

- Domain `music.example.com`, Scheme `http`, Forward Host/Port wie oben
- *Block Common Exploits* an, *Websockets Support* nicht nötig
- SSL: Let's-Encrypt-Zertifikat, **Force SSL**, **HTTP/2**, **HSTS** an
- Advanced (Uploads bis 10 MB für Importe):
  ```nginx
  client_max_body_size 12m;
  ```

NPM setzt `X-Forwarded-For` und `X-Forwarded-Proto`. Der nginx im Frontend-Container
übernimmt die Client-Adresse nur von privaten Adressen (Docker-Netze, LAN) – nötig für die
Rate-Limits je Client.

## 6. Starten

```bash
docker compose pull
docker compose up -d
docker compose ps              # db, backend, frontend: healthy; worker nach ≤ 5 min healthy
docker compose logs -f backend # "Migration abgeschlossen", "MusicHub API startet (env=production, auth=oidc …)"
```

Empfohlen direkt danach – **eigene Datenbankrolle ohne Superuser-Rechte**
([SECURITY.md](SECURITY.md#datenbank)):

```bash
# in .env: MUSICHUB_DB_USER=musichub_app und MUSICHUB_DB_PASSWORD=<zufall>
scripts/create-app-role.sh && docker compose up -d
```

## 7. Prüfen

```bash
curl -fsS https://music.example.com/api/health/ready          # {"status":"ok",…}
curl -s https://music.example.com/api/auth/session            # "mode":"oidc","login_enabled":true
curl -s -o /dev/null -w '%{http_code}\n' https://music.example.com/api/v1/tracks   # 401
curl -sI https://music.example.com/ | grep -iE 'strict-transport|content-security'
```

Im Browser `https://music.example.com` öffnen → **Anmelden** → Authentik → zurück in
MusicHub. Unter *Einstellungen → Anmeldung* stehen Name, Rechte und Sitzungsdauer.

Checkliste vor dem Freigeben:

- [ ] `.env` hat Rechte `600` und liegt nicht in Git
- [ ] Login, Abmelden und erneutes Anmelden funktionieren
- [ ] Nutzer ohne `musichub-admins` kann nur lesen
- [ ] `docker compose ps` zeigt keine Ports für `db`, `backend`, `worker`
- [ ] Backup läuft und eine Wiederherstellung wurde einmal getestet ([BACKUP.md](BACKUP.md))
- [ ] `MUSICHUB_TOKEN_ENCRYPTION_KEY` ist außerhalb der VM gesichert

## 8. Plattform-Konten (optional)

Redirect-URIs bei Spotify/Google: `https://music.example.com/api/oauth/callback/<plattform>`.
Einrichtung, Scopes und Kontingente: [CONNECTORS.md](CONNECTORS.md). Schreiben auf
Plattformen erst nach `MUSICHUB_SYNC_WRITE_PLATFORMS` ([SYNC.md](SYNC.md)).

## 9. Betrieb mit Arcane

`compose.yaml` ist Arcane-tauglich (keine Anker, kein `include`/`extends`). Projekt in
Arcane anlegen, `compose.yaml` und `.env` hinterlegen; bei Variante A zusätzlich die
Override-Datei. Arcane aktualisiert `backend`, `worker` und `frontend` bei neuen Images;
PostgreSQL ist per Label vom automatischen Update ausgenommen.

**Vor automatischen Updates** ein tägliches Backup einrichten ([BACKUP.md](BACKUP.md)). Wer
Updates lieber bewusst einspielt, setzt `MUSICHUB_VERSION` auf einen festen Tag
(`sha-<commit>` oder `vX.Y.Z`) statt `latest`.

## 10. Entwicklung vs. Produktion

| | `compose.dev.yaml` | `compose.yaml` |
|---|---|---|
| Images | lokal gebaut, Quellcode als Volume, Hot Reload | fertige Images aus GHCR |
| Anmeldung | dev-Token (`MUSICHUB_AUTH_MODE=dev`) | Authentik (OIDC), dev verboten |
| Ports | 4321, 8000, 5432 auf `127.0.0.1` | nur Frontend (`127.0.0.1:8080` oder Proxy-Netz) |
| Härtung | keine | read-only, `cap_drop: ALL`, `no-new-privileges`, Limits, internes DB-Netz |
| Swagger | an | aus (`MUSICHUB_API_DOCS=true` zum Einschalten) |
