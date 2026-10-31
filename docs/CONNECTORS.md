# Plattform-Connectoren

MusicHub liest Playlists aus verbundenen Konten bei Spotify, YouTube und (mit eigenen
Zugangsdaten) SoundCloud. **Diese Etappe ist ausschließlich lesend:** Es werden keine
Playlists, Bibliotheken oder Likes auf den Plattformen angelegt oder verändert.

Verwendet werden nur die offiziellen, dokumentierten Schnittstellen. Inoffizielle APIs,
fremde Client-IDs oder Scraping sind ausgeschlossen. Stand der Prüfung: 31.10.2026.

## Überblick

| | Spotify | YouTube | SoundCloud |
|---|---|---|---|
| Schnittstelle | Spotify Web API | YouTube Data API v3 | SoundCloud API |
| Anmeldung | OAuth 2.0 Authorization Code (Client-Secret) | Google OAuth 2.0 (Webanwendung) mit PKCE | OAuth 2.1 mit PKCE (Pflicht) |
| Scopes | `playlist-read-private`, `playlist-read-collaborative` | `https://www.googleapis.com/auth/youtube.readonly` | keine feingranularen Scopes |
| Eigene Playlists | ✔ | ✔ (Playlists des Kanals) | ✔ |
| Playlist-Inhalte | nur eigene/kollaborative (Development Mode) | ✔ | ✔ |
| Metadaten verknüpfter Tracks | ✔ (einzeln, max. 100 je Lauf) | ✔ (50 je Aufruf, max. 500 je Lauf) | – |
| Widerruf beim Trennen | nicht per API (Spotify-Konto → Apps) | ✔ (`oauth2.googleapis.com/revoke`) | nicht per API |
| Voraussetzung | App-Besitzer mit **Premium** | Google-Cloud-Projekt, OAuth-Zustimmungsbildschirm | **Artist Pro** für eine eigene API-App |

## Einrichtung

In `.env` (Werte nie ins Repository):

```bash
MUSICHUB_PUBLIC_URL=https://music.example.com      # so, wie der Browser MusicHub aufruft
MUSICHUB_TOKEN_ENCRYPTION_KEY=<32 Byte, URL-sicheres Base64>
MUSICHUB_SPOTIFY_CLIENT_ID=…
MUSICHUB_SPOTIFY_CLIENT_SECRET=…
MUSICHUB_YOUTUBE_CLIENT_ID=…
MUSICHUB_YOUTUBE_CLIENT_SECRET=…
MUSICHUB_SOUNDCLOUD_CLIENT_ID=…                    # nur mit eigener SoundCloud-App
MUSICHUB_SOUNDCLOUD_CLIENT_SECRET=…
```

Schlüssel erzeugen:
`python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
Rotation: neuen Schlüssel voranstellen, alten kommagetrennt dahinter lassen
(`neu,alt`); verschlüsselt wird mit dem ersten, entschlüsselt mit allen. Geht der Schlüssel
verloren, müssen die Konten neu verbunden werden – die Bibliothek ist davon nicht betroffen.

**Redirect-URI** je Plattform (in der App der Plattform eintragen, exakt gleich):
`<MUSICHUB_PUBLIC_URL>/api/oauth/callback/<spotify|youtube|soundcloud>`.
Die Seite *Plattformen* zeigt die fertige URI an.

### Spotify

1. developer.spotify.com/dashboard → App anlegen, „Web API“ auswählen.
2. Redirect-URI eintragen. Spotify verlangt **HTTPS**; Ausnahme ist die Loopback-Adresse
   `http://127.0.0.1:<port>` – `localhost` ist nicht erlaubt.
3. Client-ID und -Secret in `.env` übernehmen.
4. Im Development Mode unter *User Management* die Spotify-Konten eintragen, die sich
   verbinden dürfen (höchstens 5).

Einschränkungen des **Development Mode** (gilt für neue Apps seit 11.02.2026, für ältere
seit 09.03.2026, Quelle: Spotify „February 2026 Web API Dev Mode Changes – Migration Guide“):

- Der Besitzer der App braucht ein aktives **Premium**-Abo, sonst funktioniert die App nicht.
- Höchstens **5 Nutzer** pro App.
- Playlist-Inhalte (`GET /playlists/{id}/items`) nur für Playlists, die der Nutzer besitzt
  oder an denen er mitarbeitet; sonst `403`. MusicHub kennzeichnet solche Playlists als
  „Inhalt nicht lesbar“ und importiert sie nicht.
- Sammelabfragen wie `GET /tracks?ids=` entfallen – Metadaten werden pro Track einzeln
  abgerufen (deshalb höchstens 100 Tracks je Metadaten-Lauf).
- Felder wurden umbenannt (`tracks` → `items`, `track` → `item`); MusicHub liest beide.
- Rate-Limit über ein gleitendes 30-Sekunden-Fenster (Zahlen veröffentlicht Spotify nicht),
  `429` mit `Retry-After`.
- Spotify erlaubt keine Nutzung der Inhalte zum Training von ML-/KI-Modellen.

Nicht gelesen werden „Lieblingssongs“ (bräuchte zusätzlich `user-library-read`) und lokale
Dateien bekommen keinen Spotify-Link (sie werden ohne Quelle übernommen).

### YouTube

1. Google Cloud Console → Projekt → *YouTube Data API v3* aktivieren.
2. OAuth-Zustimmungsbildschirm einrichten (extern), Scope `youtube.readonly`.
3. *Anmeldedaten → OAuth-Client-ID → Webanwendung*, Redirect-URI eintragen.

Einschränkungen:

- **Kontingent:** 10 000 Einheiten pro Tag und Projekt, Zurücksetzen um Mitternacht
  pazifischer Zeit. `playlists.list`, `playlistItems.list`, `videos.list` und
  `channels.list` kosten je 1 Einheit für bis zu 50 Einträge. Ein Import einer Playlist mit
  500 Videos kostet damit etwa 20 Einheiten. Ist das Kontingent erschöpft
  (`quotaExceeded`), plant MusicHub den Job nach dem Zurücksetzen neu ein.
- Im Status **„Testing“** des Zustimmungsbildschirms laufen Refresh-Tokens nach **7 Tagen**
  ab (danach „Neu verbinden“). Für dauerhaften Betrieb die App veröffentlichen; für
  `youtube.readonly` kann Google eine Überprüfung verlangen.
- Pro Google-Konto und Client gelten höchstens 100 Refresh-Tokens; ältere verfallen.
- Konten ohne YouTube-Kanal haben keine Playlists.

**YouTube Music:** Es gibt keine offizielle YouTube-Music-API. Die Data API liefert die
YouTube-Playlists des Kanals; in YouTube Music angelegte Playlists erscheinen dort in der
Regel ebenfalls. Nicht zugänglich sind die YouTube-Music-Mediathek (Alben, Künstler,
„Mag ich“-Musik, hochgeladene Musik) sowie „Später ansehen“ und der Verlauf. MusicHub
behauptet deshalb keine YouTube-Music-Unterstützung. Gelöschte und private Videos fremder
Kanäle werden übersprungen und im Importbericht genannt; bei automatisch erzeugten
Musikkanälen („Artist - Topic“) wird der Kanalname als Artist übernommen.

### SoundCloud

Laut SoundCloud (developers.soundcloud.com/docs/api/register-app) braucht man für eine eigene
API-App und damit Client-ID/Secret ein **Artist-Pro-Abo**. MusicHub enthält keinen
Umgehungsmechanismus. Ohne Zugangsdaten:

- ist der Connector deaktiviert (Seite *Plattformen*: „Nicht eingerichtet“),
- funktionieren SoundCloud-Links in der Track-Ansicht und der CSV-/JSON-Import unverändert.

Mit Zugangsdaten ist der Connector vorbereitet: OAuth 2.1 mit PKCE, Header
`Authorization: OAuth <token>`, `GET /me/playlists`, `GET /playlists/{urn}/tracks`
(Paginierung über `linked_partitioning`/`next_href`). Refresh-Tokens sind einmalig gültig –
MusicHub speichert das neue Token sofort. Rate-Limit ca. 100 Anfragen pro Minute. Gesperrte
Tracks (`access: blocked`) werden übersprungen. Der SoundCloud-Teil ist nur gegen
nachgebildete Antworten getestet.

## Ablauf

```
Plattformen → „Mit Spotify verbinden“
  POST /api/v1/connections/spotify/authorize   state + PKCE-Verifier (verschlüsselt) speichern
  → Browser zur Plattform → Zustimmung
  GET /api/oauth/callback/spotify?code&state   state prüfen (einmalig, 10 min), Code tauschen,
                                               Tokens verschlüsselt speichern
  → Job platform.refresh_playlists             Playlist-Liste abrufen (remote_playlists)
Playlists auswählen → „importieren“
  → je Playlist ein Job platform.import_playlist
       Tracks seitenweise lesen (Fortschritt), normaler Importlauf (Importprotokoll),
       lokale Playlist anlegen bzw. ergänzen, Verfügbarkeit setzen, Matching-Scan einplanen
```

### Import-Regeln

- Vorhandene Tracks werden über Plattform-ID, ISRC und Titel/Artist/Version erkannt und nur
  **ergänzt** (leere Felder, neue Quelle) – lokal führt.
- **Mögliche Duplikate** (nur ähnlich) werden neu angelegt statt automatisch verknüpft; der
  Bereich *Matching* schlägt sie anschließend zum Zusammenführen vor.
- Beim ersten Import entsteht eine lokale Playlist gleichen Namens. Spätere Importe
  **ergänzen** sie nur: neue Einträge werden angehängt, lokal entfernte oder umsortierte
  Einträge bleiben, wie sie sind; auf der Plattform entfernte Tracks bleiben lokal erhalten.
- Jeder Import erscheint im Importprotokoll (Format „Spotify (verbundenes Konto)“ usw.) mit
  Bericht; übersprungene Einträge (nicht verfügbar, Podcast-Episoden, gelöschte Videos)
  stehen als Hinweis darin.
- **Inkrementell:** Die Playlist-Liste speichert ein Versionsmerkmal (Spotify `snapshot_id`,
  YouTube ETag + Anzahl, SoundCloud `last_modified`). Ist es seit dem letzten Import gleich,
  wird die Playlist nicht erneut gelesen („Unverändert – übersprungen“).
  „Geänderte Playlists aktualisieren“ aktualisiert die Liste und importiert nur Geändertes.
- „Metadaten aktualisieren“ prüft die am längsten nicht geprüften verknüpften Tracks:
  Verfügbarkeit wird gesetzt, leere Felder (Dauer, ISRC, Album, Datum) werden ergänzt.

## Hintergrundjobs

| Job | Payload | Wiederholung |
|---|---|---|
| `platform.refresh_playlists` | `platform`, optional `import_changed` | 5 Versuche |
| `platform.import_playlist` | `platform`, `remote_playlist_id`, optional `force` | 6 Versuche |
| `platform.refresh_metadata` | `platform`, optional `limit` | 5 Versuche |

- Gleiche Jobs werden nicht doppelt eingeplant, solange einer wartet oder läuft.
- **Fortschritt** (`jobs.progress`: `current`, `total`, `message`) wird in eigener
  Transaktion geschrieben und ist sofort sichtbar; das **Ergebnis** steht in `jobs.result`.
- **Rate-Limits:** `Retry-After` bis 5 s wartet der Client selbst, längere Wartezeiten gibt
  er an den Job (`retry_after`), der dann genau so lange später neu startet (statt des
  normalen Backoffs 30 s · 2ⁿ). YouTube-Kontingent: bis nach Mitternacht PT.
- Serverfehler (5xx, Zeitüberschreitung) werden direkt bis zu zweimal wiederholt, danach
  übernimmt das Job-Backoff.
- **Fehler:** `last_error` am Job, `last_error`/`last_import_status = error` an der
  Playlist. Abgelaufene/widerrufene Anmeldung → Konto „Neu verbinden nötig“, der Job endet
  ohne Wiederholung.

## Sicherheit

- Tokens liegen nur verschlüsselt in `connector_accounts` (Fernet: AES-128-CBC +
  HMAC-SHA256). Sie erscheinen nie in API-Antworten, Logs oder Fehlermeldungen;
  `OAuthTokens.__repr__` blendet sie aus. httpx-Logs sind auf `WARNING` gesetzt, das
  Access-Log enthält keine Query-Strings.
- `state`: 256 Bit zufällig, nur als SHA-256 gespeichert, einmalig, 10 Minuten gültig, an
  die Plattform gebunden. Der Rückruf antwortet mit `Cache-Control: no-store` und
  `Referrer-Policy: no-referrer`.
- Der HTTP-Client der Connectoren lässt gegenüber den Plattform-APIs **nur GET** zu und
  sendet Tokens nur an den eigenen API-Host (fremde `next`-Links werden abgelehnt). POST
  gibt es nur an Token-/Widerrufs-Endpunkte. Ein Test prüft das für alle drei Connectoren.
- Token-Erneuerung läuft in eigener Transaktion mit Zeilensperre (einmalige Refresh-Tokens
  gehen so nie durch einen Rollback verloren).
- Trennen löscht die Tokens (YouTube: zusätzlich Widerruf bei Google). Importierte Tracks,
  Playlists und Links bleiben.

## Tests

`tests/fake_platforms.py` bildet die drei APIs nach (Antwortformate laut offizieller
Referenz). `test_connectors.py` prüft Connectoren, HTTP-Client, Rate-Limits, Kontingent,
Verschlüsselung und „nur GET“; `test_connections_api.py` den kompletten Ablauf über API und
Worker gegen PostgreSQL (Verbinden, falscher/wiederverwendeter `state`, Import,
inkrementelle Aktualisierung, Duplikaterkennung, Rate-Limit mit Neuplanung, Token-Erneuerung
und Widerruf, Trennen, Metadaten). Die CI spricht nie echte Plattformen an.

## Quellen

- Spotify: developer.spotify.com/documentation/web-api – February 2026 Migration Guide,
  Get Playlist Items, Get Current User's Playlists, Authorization Code Flow, Redirect URIs,
  Rate Limits
- YouTube: developers.google.com/youtube/v3 – Quota Calculator (determine_quota_cost),
  playlists.list, playlistItems.list; developers.google.com/identity/protocols/oauth2
- SoundCloud: developers.soundcloud.com/docs/api – Register App, API Guide
  (Authentication, Pagination), Rate Limits, API Explorer (OpenAPI)
