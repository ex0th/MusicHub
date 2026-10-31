# Import und Export

MusicHub liest Bibliotheken und Playlists aus mehreren Dateiformaten ein und exportiert die
eigene Bibliothek so, dass sie sich verlustfrei wieder einlesen lässt. Beispieldateien liegen
unter [`examples/import/`](../examples/import/).

## Ablauf eines Imports

| Schritt | Was passiert | API |
|---|---|---|
| 1. Datei hochladen | Datei wird geprüft (Typ, Größe, Kodierung, Binärinhalt) | `POST /api/v1/imports` |
| 2. Format erkennen | passender Adapter anhand Endung und Kopfzeile bzw. JSON-Aufbau | (Teil von 1) |
| 3. Datensätze analysieren | jede Zeile wird in ein neutrales Format übersetzt und geprüft | (Teil von 1) |
| 4. Vorschau | Datensätze mit Status, Fehlern und Hinweisen | `GET /api/v1/imports/{id}/items` |
| 5./6. Abgleich | neu, bereits vorhanden, mögliches Duplikat, doppelt in der Datei, fehlerhaft | (Teil von 1) |
| Anpassen | Entscheidung je Datensatz, Umgang mit Playlists | `PATCH …/items/{item}`, `PATCH /api/v1/imports/{id}` |
| 7. Bestätigen | Tracks, Quellen, Tags und Playlists werden geschrieben | `POST /api/v1/imports/{id}/commit` |
| Bericht | Ergebnis jedes Datensatzes als CSV oder JSON | `GET /api/v1/imports/{id}/report?format=csv` |

Bis zur Bestätigung ändert sich an der Bibliothek nichts. Ein Import kann verworfen werden
(`POST …/discard`). Jeder Vorgang – auch abgelehnte Dateien – steht im Importprotokoll
(`GET /api/v1/imports`, Tabellen `import_runs`/`import_items`).

### Status je Datensatz

| Status | Bedeutung | Vorgeschlagene Aktion |
|---|---|---|
| `new` | kein passender Track in der Bibliothek | neu anlegen |
| `existing` | eindeutig erkannt (MusicHub-ID, Plattform-Link, ISRC oder Titel+Artist+Version) | vorhandenen verwenden |
| `potential_duplicate` | ähnlicher Track desselben Artists (Ähnlichkeit ≥ 75 %) | ab 92 %: vorhandenen verwenden, sonst neu anlegen |
| `file_duplicate` | derselbe Track steht weiter oben in der Datei | folgt der Entscheidung für das erste Vorkommen |
| `invalid` | Pflichtfeld fehlt oder Wert unbrauchbar | wird übersprungen, erscheint im Bericht |

Fehlerhafte Datensätze brechen den Import nie ab. Unbrauchbare **optionale** Werte (z. B. eine
ungültige ISRC oder ein unbekannter Link) werden verworfen und als Hinweis gemeldet, der
Datensatz bleibt gültig.

### Erkennen vorhandener Tracks

In dieser Reihenfolge, das erste Merkmal gewinnt (`transfer/matching.py`):

1. `musichub_id` aus einem MusicHub-Export
2. bestätigte Plattform-Quelle (Plattform + ID, z. B. Spotify-Track-ID)
3. ISRC
4. Identitätsschlüssel: normalisierter Titel, Artists und Version. „Innerbloom (Extended Mix)“,
   „Innerbloom - Extended Mix“ und Titel „Innerbloom“ mit Version „Extended Mix“ sind gleich;
   „Original Mix“ zählt als keine Version; verschiedene Remixe bleiben verschieden.

Ohne exakten Treffer prüft die Matching-Engine ähnliche Tracks desselben Artists.

### Was beim Bestätigen passiert

- **Neu anlegen:** Track mit allen Metadaten, Plattform-Links (Methode `import`) und Tags.
  Direkt vorher wird erneut abgeglichen – wurde der Track inzwischen angelegt, wird er verknüpft.
- **Vorhandenen verwenden:** Die lokale Bibliothek führt. Es werden nur **leere** Felder ergänzt
  (Album, Version, Dauer, ISRC, Veröffentlichung), neue Plattform-Links und Tags hinzugefügt.
  Hat der Track schon eine bestätigte Quelle derselben Plattform, wird der neue Link als
  Vorschlag gespeichert. Gehört ein Link bereits zu einem anderen Track, wird er übersprungen.
- **Playlists** je Name: *Neue Playlist anlegen*, *Fehlende Tracks ergänzen* (Standard, wenn es
  eine gleichnamige Playlist gibt), *Inhalt ersetzen* oder *Nicht importieren*. Datensätze ohne
  Playlist-Angabe können als eigene Playlist angelegt werden (Name = Dateiname; Standard bei
  Formaten, die typischerweise eine Playlist sind: TXT, Soundiiz, Exportify, TuneMyMusic).
- **Tags** im Format `Kategorie:Name` werden angelegt, falls es sie noch nicht gibt.

Alles läuft in einer Datenbanktransaktion; jeder Datensatz zusätzlich in einem eigenen
Savepoint. Scheitert ein einzelner Datensatz, wird nur er zurückgerollt und als
`failed` protokolliert. Ein Import lässt sich nur einmal bestätigen (Sperre auf dem Importlauf).

### Wiederholte Imports

Dieselbe Datei erneut zu importieren erzeugt keine Duplikate: Alle Tracks werden über die
Merkmale oben als `existing` erkannt, Playlists stehen auf *Fehlende Tracks ergänzen* und
bekommen nichts doppelt. Zusätzlich wird anhand der SHA-256-Prüfsumme gewarnt, wenn genau
diese Datei schon einmal importiert wurde.

## Sicherheit und Grenzen

| Prüfung | Verhalten |
|---|---|
| Dateityp | nur `.csv`, `.tsv`, `.txt`, `.json`; Pfadanteile im Dateinamen werden entfernt |
| Größe | Standard 10 MB (`MUSICHUB_IMPORT_MAX_BYTES`), geprüft am tatsächlichen Inhalt; nginx begrenzt Uploads auf 12 MB → `413` |
| Datensätze | Standard 20 000 (`MUSICHUB_IMPORT_MAX_ROWS`) |
| Kodierung | UTF-8 (mit/ohne BOM), UTF-16 mit BOM; Windows-1252 als Notlösung mit Hinweis |
| Inhalt | NUL-Bytes und Steuerzeichen → Datei abgelehnt (keine Textdatei); Feldgröße begrenzt |
| Rechte | Hochladen, Entscheiden, Bestätigen nur mit Schreibrecht; Protokoll und Bericht lesend |
| Export | CSV-Zellen, die mit `=`, `+`, `-`, `@`, Tab oder CR beginnen, bekommen ein `'` vorangestellt (Formula-Injection); MusicHub-Import entfernt es wieder |

Hochgeladene Dateien werden nicht gespeichert, nur die eingelesenen Datensätze im Protokoll.

## Formate

Spaltennamen werden ohne Groß-/Kleinschreibung und mit vereinheitlichten Leerzeichen und
Bindestrichen verglichen. Trennzeichen (`,` `;` Tab `|`) werden an der Kopfzeile erkannt,
Werte in Anführungszeichen dürfen Trennzeichen und Zeilenumbrüche enthalten. Spalten, die
nicht aufgeführt sind, werden ignoriert. Das Format wird automatisch erkannt, kann aber im
Assistenten bzw. per `format`-Feld erzwungen werden.

### TuneMyMusic CSV (`tunemymusic_csv`)

Erkannt an `Track name` + `Artist name` zusammen mit `Playlist name` oder `Type`.

| Spalte | Feld |
|---|---|
| `Track name` | Titel |
| `Artist name` | Artist |
| `Album` | Album |
| `Playlist name` | Playlist (mehrere Playlists in einer Datei möglich) |
| `Type` | wird nicht ausgewertet |
| `ISRC` | ISRC |
| `Spotify - id`, `YouTube - id`, `SoundCloud - id` | Plattform-ID |

Andere `<Plattform> - id`-Spalten werden mit Hinweis ignoriert.
**Quelle:** Die Spaltenliste stammt nicht aus einer offiziellen Spezifikation von TuneMyMusic,
sondern aus der Importdokumentation von [crates.app](https://crates.app/knowledge-base/import-from-csv-how-does-it-work/)
(die TuneMyMusic-Exporte verarbeitet) und deckt sich mit einem
[Erfahrungsbericht](https://jeremygarnett.substack.com/p/exod_us-spotify-heading-to-tidal).
SoundCloud-IDs in TuneMyMusic-Dateien sind vermutlich numerisch; MusicHub speichert
SoundCloud-Quellen bisher als Permalink und überspringt solche IDs mit Hinweis.

### Soundiiz CSV (`soundiiz_csv`)

Erkannt, wenn die Kopfzeile **nur** aus `title`, `artist`, `album`, `isrc` besteht
(mindestens `title`). Trennzeichen Komma oder Semikolon.
**Quelle:** [Soundiiz-Hilfe zum CSV-Format](https://support.soundiiz.com/hc/en-us/articles/360010006793-What-is-the-CSV-format-to-import-playlists-and-favorites)
(Kopfzeile Pflicht, `title` Pflicht, `,` oder `;`). Eine Beispiel-Exportdatei von Soundiiz
war nicht abrufbar; enthält ein echter Export weitere Spalten, greift der allgemeine
CSV-Adapter mit denselben Feldern. Ohne `artist` sind Datensätze fehlerhaft, weil MusicHub
für jeden Track einen Artist braucht.

### Exportify CSV (`exportify_csv`)

Spotify-Playlist-Export von [Exportify](https://github.com/watsonbox/exportify); Spalten laut
dessen README. Erkannt an `Track URI` + `Track Name` + `Artist Name(s)`.

| Spalte | Feld |
|---|---|
| `Track URI` | Spotify-Quelle (`spotify:local:…` wird mit Hinweis übersprungen) |
| `Track Name` | Titel |
| `Artist Name(s)` | Artist |
| `Album Name` | Album |
| `Album Release Date` | Veröffentlichung (nur vollständige Daten; reine Jahreszahl → Hinweis) |
| `Track Duration (ms)` | Dauer |
| `ISRC` | ISRC |

### Allgemeines CSV (`generic_csv`)

Für eigene Tabellen. Nötig sind eine Titel- und eine Artist-Spalte.

| Feld | Erkannte Spaltennamen |
|---|---|
| Titel* | `title`, `track`, `track name`, `track title`, `song`, `song name`, `name`, `titel`, `songtitel` |
| Artist* | `artist`, `artists`, `artist name`, `artist name(s)`, `artist(s)`, `interpret`, `künstler` |
| Album | `album`, `album name`, `album title` |
| Version | `version`, `mix`, `remix` |
| Dauer | `duration`, `length`, `time`, `dauer`, `länge` (`3:45`, `1:02:03` oder Sekunden; Zahlen über 10 000 gelten als Millisekunden) · `duration (ms)`, `duration_ms`, `track duration (ms)` · `duration_s`, `seconds` |
| ISRC | `isrc` (Bindestriche/Leerzeichen erlaubt) |
| Veröffentlichung | `release date`, `release_date`, `album release date`, `released`, `veröffentlicht` (`JJJJ-MM-TT` oder `TT.MM.JJJJ`) |
| Links (Plattform wird erkannt) | `url`, `urls`, `link`, `links`, `uri`, `track uri` – mehrere durch Leerzeichen oder `\|` getrennt |
| Links/IDs je Plattform | `spotify url`, `spotify_url`, `spotify id`, `spotify - id` (analog für `youtube` und `soundcloud`) |
| Playlist | `playlist`, `playlist name`, `playlist_name` |
| Position | `position`, `playlist_position`, `#`, `nr` (sonst Zeilenreihenfolge) |
| Tags | `tags` (`Kategorie:Name`, getrennt durch `\|` oder `;`), `genre` (→ Tag „Genre:…“) |
| Notizen | `notes`, `notizen`, `comment`, `kommentar` |

### JSON (`generic_json`)

Drei Aufbauten werden akzeptiert; die Felder je Track heißen wie die CSV-Spalten oben
(`title`, `artist`, `duration`, `spotify_url`, `tags` als Liste …):

```json
[ { "title": "Teardrop", "artist": "Massive Attack" } ]
{ "name": "Abend", "tracks": [ { "title": "…", "artist": "…" } ] }
{ "playlists": [ { "name": "Abend", "description": "…", "tracks": [ … ] } ], "tracks": [ … ] }
```

Im dritten Fall stehen Tracks unter `tracks` ohne Playlist in der Bibliothek.

### Textliste (`txt`)

Eine Zeile pro Track im Muster `Artist - Titel` (auch `–` oder `—`, jeweils mit Leerzeichen).
Optional: Nummerierung am Anfang (`1.` oder `1)`), Dauer am Ende (`3:45`, `(3:45)`, `[3:45]`),
Links irgendwo in der Zeile. Leere Zeilen und Zeilen mit `#` am Anfang werden übersprungen.
Zeilen ohne erkennbaren Artist oder nur mit Link sind fehlerhaft.

### MusicHub CSV und JSON (`musichub_csv`, `musichub_json`)

Das Exportformat von MusicHub (siehe unten). Tracks werden beim Wiederimport über ihre
`musichub_id` bzw. `id` erkannt; Tags, alle bestätigten Plattform-Links und Playlists mit
Reihenfolge werden übernommen.

## Export

| Was | API | Inhalt |
|---|---|---|
| Gesamte Bibliothek | `GET /api/v1/export/library?format=csv\|json` | alle Tracks, Plattform-Links, Tags, Playlists |
| Eine Playlist | `GET /api/v1/playlists/{id}/export?format=csv\|json` | Tracks der Playlist in Reihenfolge |

In der Oberfläche: *Musikbibliothek → Import und Export* bzw. *Export CSV/JSON* auf der
Playlist-Seite. Dateinamen: `musichub-bibliothek-JJJJ-MM-TT.csv`,
`playlist-<name>-JJJJ-MM-TT.json`.

### MusicHub CSV

UTF-8 mit BOM (Excel erkennt Umlaute), Komma, CRLF. **Eine Zeile je Playlist-Eintrag**;
Tracks, die in keiner Playlist stehen, haben eine Zeile mit leerer `playlist`. Ein Track in
drei Playlists steht also dreimal in der Datei – beim Import wird er einmal angelegt.

| Spalte | Inhalt |
|---|---|
| `musichub_id` | Track-ID |
| `title`, `artist`, `version`, `album` | Metadaten |
| `duration_ms` | Dauer in Millisekunden |
| `isrc`, `release_date` | ISRC, Datum `JJJJ-MM-TT` |
| `tags` | `Kategorie:Name`, getrennt durch `\|` |
| `spotify_url`, `soundcloud_url`, `youtube_url` | bestätigte Plattform-Links |
| `playlist`, `playlist_position` | Playlist-Name und Position (ab 1) |
| `notes` | Notizen |

### MusicHub JSON

```json
{
  "format": "musichub",
  "version": 1,
  "exported_at": "2026-10-31T15:00:00+00:00",
  "scope": "library",
  "tracks": [
    {
      "id": "…", "title": "Teardrop", "artist": "Massive Attack", "version": null,
      "album": "Mezzanine", "duration_ms": 330000, "isrc": "GBAAA9800058",
      "release_date": null, "notes": null, "tags": ["Stimmung:Abend"],
      "sources": [ { "platform": "spotify", "external_id": "…", "url": "…",
                     "link_status": "linked", "availability": "available", "last_checked_at": null } ],
      "created_at": "…", "updated_at": "…"
    }
  ],
  "playlists": [ { "id": "…", "name": "Sonntagabend", "description": null, "tracks": ["<track-id>", "…"] } ]
}
```

JSON enthält zusätzlich Vorschläge und abgelehnte Quellen sowie die Verfügbarkeit; beim
Import werden nur bestätigte Quellen übernommen.

## Beispieldateien

| Datei | Format | Zeigt |
|---|---|---|
| `tunemymusic.csv` | TuneMyMusic | zwei Playlists, ein Track in beiden |
| `soundiiz.csv` | Soundiiz | Minimalformat mit ISRC |
| `exportify.csv` | Exportify | alle Spalten, lokale Spotify-Datei |
| `bibliothek.csv` | allgemein | Semikolon, deutsche Spaltennamen, Position, Genre, fehlerhafte Zeile |
| `tracklist.txt` | Textliste | Nummern, Dauer, Link, Kommentar, zwei fehlerhafte Zeilen |
| `tracks.json` | JSON | Playlist mit Beschreibung plus Track ohne Playlist |

IDs, ISRCs und Links in den Beispielen sind zur Veranschaulichung gewählt und zeigen nicht
zwingend auf die genannten Titel.
