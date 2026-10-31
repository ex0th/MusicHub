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
`unprocessable` (fachlich ungültig, z. B. unbekannte Tag-IDs, mit Zusatzfeldern),
`service_unavailable`, `internal_error` (u. a.). Bei `500` enthält `detail` bewusst keine
internen Informationen – über `request_id` findet man den Eintrag im Server-Log.

## Endpunkte

Alle Pfade unter `/api/v1` brauchen ein Token; schreibende zusätzlich Schreibrecht.
Request-Bodies lehnen unbekannte Felder ab (`422`), leere Strings in optionalen Textfeldern
werden zu `null`.

### Health und System

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/health/live` | Prozess läuft. `{status, version, build}` – öffentlich |
| GET | `/api/health/ready` | DB erreichbar und Schema aktuell, sonst `503` – öffentlich |
| GET | `/api/v1/me` | Angemeldeter Benutzer |
| GET | `/api/v1/connectors` | Zustand der Plattform-Connectoren |
| GET | `/api/v1/stats` | Kennzahlen fürs Dashboard (siehe unten) |

### Tracks

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/tracks` | Suchen, filtern, sortieren, paginieren |
| POST | `/api/v1/tracks` | Anlegen, optional mit `tag_ids` und `sources` → `201` |
| GET | `/api/v1/tracks/{id}` | Detail inkl. `sources`, `tags`, `platforms`, `playlists` |
| PATCH | `/api/v1/tracks/{id}` | Teiländerung; `null` leert optionale Felder; `tag_ids` ersetzt alle Tags |
| DELETE | `/api/v1/tracks/{id}?force=` | `204`; steckt der Track in Playlists → `409` mit `playlists`, mit `force=true` wird er dort entfernt |
| PUT | `/api/v1/tracks/{id}/tags` | Tags setzen: `{"tag_ids": [...]}` |

Felder: `title`*, `artist`*, `album`, `version`, `duration_ms` (> 0), `isrc` (wird
normalisiert, `us-um7-19-00001` → `USUM71900001`), `release_date` (`YYYY-MM-DD`), `notes`.
Antwort zusätzlich: `platforms` = Plattformen mit bestätigter, nicht unverfügbarer Quelle.

**Suche und Filter** (`GET /api/v1/tracks`, kombinierbar):

| Parameter | Bedeutung |
|---|---|
| `q` | Wörter, die alle in Titel, Artist, Album, Version oder ISRC vorkommen müssen |
| `platform` (mehrfach) | verfügbar auf **allen** genannten Plattformen |
| `missing_platform` (mehrfach) | auf keiner der genannten verfügbar |
| `platform_count` | genaue Anzahl verfügbarer Plattformen (`0` = nur lokal, `1` = genau eine) |
| `tag` (mehrfach, UUID) | hat **alle** genannten Tags |
| `has_isrc` | `true`/`false` |
| `sort` | `title`, `artist` (Standard), `album`, `version`, `duration`, `release_date`, `created_at`, `updated_at` |
| `order` | `asc` (Standard) / `desc`; leere Werte stehen immer am Ende |
| `limit`, `offset` | 1–500 (Standard 50) |

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/tracks?platform=spotify&missing_platform=youtube&sort=duration&order=desc"
```

### Track-Quellen

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/tracks/{id}/sources` | Alle Quellen des Tracks |
| POST | `/api/v1/tracks/{id}/sources` | Hinzufügen → `201` |
| PATCH | `/api/v1/tracks/{id}/sources/{source_id}` | `url`/`external_id`, `availability`, `link_status`, `last_checked_at` |
| DELETE | `/api/v1/tracks/{id}/sources/{source_id}` | Entfernen → `204` |

Hinzufügen entweder per Link (Plattform und ID werden erkannt) oder per Plattform + ID:

```json
{ "url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", "availability": "available" }
{ "platform": "youtube", "external_id": "dQw4w9WgXcQ" }
```

Erkannt werden `open.spotify.com/track/…`, `spotify:track:…`, `youtube.com/watch?v=…`,
`youtu.be/…`, `music.youtube.com/…`, `youtube.com/shorts/…` und `soundcloud.com/<nutzer>/<track>`.
Ohne `availability` ist sie `unknown`; ändert sich `availability`, wird `last_checked_at`
automatisch gesetzt. Fehler: `422` (Link/ID ungültig oder passt nicht zur Plattform),
`409` (Track hat schon eine bestätigte Quelle dieser Plattform, oder der Plattform-Track
ist bereits einem anderen Track zugeordnet – dann mit `track_id`).

### Tags

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/tags?q=&category=` | Alle Tags mit `track_count` |
| POST | `/api/v1/tags` | `{"name": "House", "category": "Genre"}` → `201`; Duplikat → `409` |
| PATCH | `/api/v1/tags/{id}` | Umbenennen / Kategorie ändern |
| DELETE | `/api/v1/tags/{id}` | `204`; Tracks bleiben erhalten |

### Playlists

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/playlists?q=&limit=&offset=` | Übersicht mit `item_count`, `total_duration_ms` |
| POST | `/api/v1/playlists` | `{name, description?, track_ids?}` → `201` |
| GET | `/api/v1/playlists/{id}` | Detail mit allen Einträgen (`items[]`: `id`, `position`, `added_at`, `track`) |
| PATCH | `/api/v1/playlists/{id}` | Name/Beschreibung |
| DELETE | `/api/v1/playlists/{id}` | `204`; Tracks bleiben erhalten |
| POST | `/api/v1/playlists/{id}/items` | `{track_ids, position?, skip_existing?}` – anhängen oder an Position einfügen |
| DELETE | `/api/v1/playlists/{id}/items/{item_id}` | Eintrag entfernen |
| POST | `/api/v1/playlists/{id}/items/{item_id}/move` | `{"position": 0}` – verschieben (größer als Ende = ans Ende) |
| PUT | `/api/v1/playlists/{id}/items/order` | `{"item_ids": [...]}` – komplette Reihenfolge, jede Eintrags-ID genau einmal |

Alle Änderungen an Einträgen antworten mit der vollständigen Playlist, Positionen sind
immer lückenlos `0..n-1`. Einträge werden über ihre `item_id` angesprochen, nicht über die
Track-ID – derselbe Track darf mehrfach in einer Playlist stehen.

### Statistik

`GET /api/v1/stats`:

```json
{
  "tracks": 13, "playlists": 3, "tags": 5,
  "local_only": 3,
  "single_platform": 3,
  "platforms": [
    { "platform": "spotify", "tracks": 8, "exclusive": 2, "unavailable": 0 },
    { "platform": "soundcloud", "tracks": 5, "exclusive": 1, "unavailable": 0 },
    { "platform": "youtube", "tracks": 6, "exclusive": 0, "unavailable": 1 }
  ]
}
```

`tracks` je Plattform = Tracks mit verfügbarer bestätigter Quelle, `exclusive` = davon nur
dort verfügbar, `unavailable` = bestätigte Quellen mit `availability = unavailable`.

## Neue Endpunkte – Checkliste

1. Router unter `api/routes/`, in `main.py` einbinden.
2. Lesend: `Depends(current_principal)`; schreibend: `Depends(require_write)`.
   Der Routen-Test schlägt sonst fehl.
3. Eigene Pydantic-Schemas in `api/schemas.py` (Bodies erben von `InputModel`), Fachlogik in
   `musichub/library/` – Routen bleiben dünn und committen am Ende.
4. Fachliche Fehler als `AppError`-Unterklasse werfen, nicht `HTTPException`.
5. Diese Datei und – falls nötig – `frontend/src/lib/types.ts` aktualisieren.
