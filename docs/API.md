# API

REST-API unter `/api`. Die vollständige, stets aktuelle Beschreibung erzeugt FastAPI:

- OpenAPI-JSON: `/api/openapi.json`
- Swagger-UI: `/api/docs`

## Grundregeln

- Versionierte Fachendpunkte unter `/api/v1`, Betriebsendpunkte unter `/api/health`.
- JSON, Zeitstempel ISO 8601 mit Zeitzone, IDs als UUID.
- Listen sind paginiert: `?limit=` (1–200, Standard 50) und `?offset=`; Antwort
  `{ items, total, limit, offset }`.
- Jede Antwort trägt `X-Request-ID` (wird übernommen, wenn der Client/nginx sie mitschickt).

## Authentifizierung

`Authorization: Bearer <token>`

| Bereich | Anforderung |
|---|---|
| `/api/health/*`, `/api/openapi.json`, `/api/docs` | öffentlich |
| `GET /api/v1/*` | angemeldet |
| `POST`/`PATCH`/`PUT`/`DELETE /api/v1/*` | angemeldet **und** Schreibrecht |

Token je nach `MUSICHUB_AUTH_MODE`: Entwicklungs-Token (`dev`) oder OIDC-Access-Token
(`oidc`, z. B. von Authentik). Schreibrecht im OIDC-Modus über Gruppe
`MUSICHUB_OIDC_WRITE_GROUP`. Fehlt oder ist das Token ungültig: `401` mit
`WWW-Authenticate: Bearer`; fehlt das Schreibrecht: `403`.

## Fehlerformat (RFC 9457)

```json
{
  "type": "https://musichub.local/problems/not_found",
  "title": "Not Found",
  "status": 404,
  "code": "not_found",
  "detail": "Track 3f… nicht gefunden.",
  "request_id": "9b1c…"
}
```

Content-Type `application/problem+json`. Bei `422` zusätzlich `errors: [{loc, msg, type}]`.
`code`-Werte: `unauthorized`, `forbidden`, `not_found`, `conflict`, `validation_error`,
`service_unavailable`, `internal_error` (u. a.). Bei `500` enthält `detail` bewusst keine
internen Informationen – über `request_id` findet man den Eintrag im Server-Log.

## Endpunkte (Etappe 1)

### Health

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/health/live` | Prozess läuft. `{status, version, build}` |
| GET | `/api/health/ready` | DB erreichbar und Schema aktuell. `200` oder `503` mit `checks.database` / `checks.migrations` |

### System

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/me` | Angemeldeter Benutzer: `subject`, `display_name`, `groups`, `can_write`, `auth_method` |
| GET | `/api/v1/connectors` | Status je Plattform: `state` (`not_configured`/`ready`/`degraded`/`suspended`), `capabilities`, `last_error` |

### Tracks

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/tracks?q=&limit=&offset=` | Liste/Suche in Titel, Interpret, Album |
| GET | `/api/v1/tracks/{id}` | Einzelner Track inkl. `links` |
| POST | `/api/v1/tracks` | Anlegen → `201` |
| PATCH | `/api/v1/tracks/{id}` | Teilweise ändern |
| DELETE | `/api/v1/tracks/{id}` | Löschen → `204`; `409`, wenn der Track in Playlists steckt |

Track-Felder: `title`*, `artist`*, `album`, `duration_ms` (> 0), `isrc` (wird
normalisiert, z. B. `us-um7-19-00001` → `USUM71900001`), `notes`.

```bash
curl -X POST http://localhost:8000/api/v1/tracks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"Teardrop","artist":"Massive Attack","duration_ms":330000}'
```

## Neue Endpunkte – Checkliste

1. Router unter `api/routes/`, in `main.py` einbinden.
2. Lesend: `Depends(current_principal)`; schreibend: `Depends(require_write)`.
   Der Routen-Test schlägt sonst fehl.
3. Eigene Pydantic-Schemas in `api/schemas.py`, keine ORM-Objekte direkt erwarten.
4. Fachliche Fehler als `AppError`-Unterklasse werfen, nicht `HTTPException`.
5. Diese Datei und – falls nötig – `frontend/src/lib/types.ts` aktualisieren.
