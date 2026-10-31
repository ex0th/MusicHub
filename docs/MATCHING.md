# Matching

Erkennt lokale Tracks, die **dieselbe Aufnahme in derselben Version** sind – typischerweise,
weil ein Song einzeln von Spotify, SoundCloud und YouTube in die Bibliothek kam:

| Plattform | Eintrag |
|---|---|
| Spotify | Artist – Track Name (Original Mix) |
| SoundCloud | Artist – Track Name |
| YouTube | „Artist - Track Name [Official Audio]“ auf dem Kanal „Artist - Topic“ |

Diese drei werden erkannt. **Nie automatisch** zusammengeführt werden dagegen verschiedene
Versionen: Original Mix, VIP Mix, Bootleg, Remix (auch zwei Remixe verschiedener Bearbeiter),
Radio Edit, Extended Mix, Club Mix, Dub, Live, Cover, Akustik, Instrumental, Sped up/Slowed,
Remaster, Neuaufnahme („Taylor's Version“), Demo, Mashup, Karaoke.

## Bausteine

| Ort | Aufgabe |
|---|---|
| `matching/engine.py` | reine Bewertung zweier Tracks: `parse()`, `compare()` → `MatchAssessment` |
| `matching/blocking.py` | reine Kandidatensuche ohne Vergleich jeder mit jedem |
| `library/matching.py` | Scan, Vorschläge lesen, Entscheidungen, automatische Zusammenführung |
| `library/merge.py` | Zusammenführen und Rückgängigmachen mit Protokoll |
| `api/routes/matching.py` | REST-API unter `/api/v1/matching` |
| `frontend/src/pages/matching/` | Bereich „Matching“ |
| Job `matching.scan` | wird nach jedem bestätigten Import eingeplant |

Die Engine wird auch beim Import für „mögliche Duplikate“ verwendet (`transfer/matching.py`).

## Ablauf der Bewertung

### 1. Zerlegen (`parse`)

- **Normalisieren:** Groß-/Kleinschreibung, Akzente (`RÜFÜS` = `rufus`), Apostrophe,
  Satzzeichen, `&` = `and`, `Pt.` = `Part`, führendes „The“ bei Artists.
- **Zusätze entfernen:** „Official Audio/Video“, „Lyric Video“, „Visualizer“, „HD/4K“,
  „Free Download“, „Premiere:“, „prod. by …“, „| Official Video“, Kanalnamen „- Topic“,
  „VEVO“, „Official“.
- **Featuring:** „feat./ft./featuring/with“ im Titel oder Artist-Feld wird als Mitwirkender
  erkannt und aus dem Titel entfernt.
- **Versionen:** aus dem Versionsfeld, aus Klammern und aus „ - …“ am Titelende. Ein Teil nach
  „ - “ gilt nur als Version, wenn er auf ein Versionswort endet („- Radio Edit“, „- Live“,
  „- Remastered 2011“) – „Oasis - Live Forever“ bleibt ein Titel. „Original Mix“, „Original“
  und „Album Version“ bedeuten *Original*. Bei Remix, Bootleg, Edit, VIP und Dub wird der
  Bearbeiter erkannt („Chromatics Remix“, „Remix by Kaytranada“).
- **Zwei Lesarten:** Steht im Titel „A - B“, wird zusätzlich „Artist A, Titel B“ geprüft
  (YouTube-Uploads auf fremden Kanälen).
- **Unbekannte Tracks:** Ohne verwertbaren Titel oder Artist („Track 01“, „Unknown Artist“,
  „Various Artists“, „Untitled“) wird ein Track **nie** zugeordnet und bleibt separat.

### 2. Confidence Score (0–100)

Der Wert entsteht in nachvollziehbaren Schritten; jeder Schritt erscheint als Grund
(`reasons`: `code`, `text`, `effect`, `kind` = `plus` | `minus` | `info` | `cap` | `block`).

**Grundwert** = Titel (max. 55) + Artist (max. 45)

| Titel | Punkte | | Artist | Punkte |
|---|---|---|---|---|
| identisch | 55 | | identisch | 45 |
| bis auf Leerzeichen gleich | 54 | | gleich, Mitwirkende abweichend | 40–45 |
| ähnlich (≥ 80 %) | anteilig | | bis auf Schreibweise gleich | 43 |
| | | | nur ähnlicher Name (Bonobo/Bonobos) | 20 |

**Zu- und Abschläge**

| Merkmal | Wirkung |
|---|---|
| gleiche ISRC | +30 |
| verschiedene ISRC | −10 |
| Dauer ±2 s / ±5 s | +4 / +1 |
| Dauer 5–15 s / 15–30 s / > 30 s abweichend | −5 / −12 / −25 |
| Videofassung („Official Video“) bis 90 s länger | −8 statt −25 |
| gleiches Album / gleiches Jahr | +2 / +2 |
| Veröffentlichung > 1 Jahr auseinander | −6 |
| widersprüchliche Featuring-Artists | −10 |
| Klammerzusatz nur auf einer Seite | −4 |
| gleicher Plattform-Track (gleiche ID) | +30 |
| je ein *anderer* Track derselben Plattform | −5 und Blocker |

**Obergrenzen (Caps)** – kein Bonus hebt sie auf:

| Situation | max. |
|---|---|
| unbekannter Titel/Artist | 0 |
| zwei Remixe verschiedener Bearbeiter | 30 |
| verschiedene Versionen (Remix ≠ Original, Radio Edit ≠ Extended Mix, „Original Mix“ ≠ Extended Mix …) | 35 |
| Version, die praktisch immer im Titel steht (Remix, VIP, Bootleg, Live, Cover, Akustik, Sped up …), gegenüber unbeschriftet | 40 |
| verschiedene Teil-/Nummernangabe („Part 1“/„Part 2“) | 45 |
| Titel unterschiedlich / Artist unterschiedlich | 50 |
| Dauer > 30 s abweichend | 60 |
| nur ähnlicher Artist-Name | 70 |
| Radio Edit/Extended/Club/Edit gegenüber unbeschriftet | 75 |
| Bearbeiter nur auf einer Seite angegeben | 80 |
| gleiche ISRC, aber Titel passt nicht | 80 |
| verschiedene ISRC; Dauer 15–30 s abweichend; Videofassung; Titel nur ähnlich | 84 |
| Remaster gegenüber Original | 88 |
| kein harter Beleg; jeder Blocker | 94 |

### 3. Stufen

| Stufe | Confidence | Bedingung | Bedeutung |
|---|---|---|---|
| `certain` – Sicher identifiziert | ≥ 95 | harter Beleg **und** kein Blocker | darf automatisch zusammengeführt werden |
| `likely` – Wahrscheinlich identisch | 85–94 | | Vorschlag, Bestätigung nötig |
| `review` – Manuelle Prüfung notwendig | 60–84 | | Vorschlag, genau prüfen |
| `different` – Unterschiedliche Tracks | < 60 | | wird nicht vorgeschlagen |

**Harter Beleg** ist eine gleiche ISRC (bei gleicher Version), dieselbe Plattform-ID oder
exakt gleicher Titel, Artist und Version **mit** Dauer ±2 s. Fehlt beides (z. B. keine ISRC
und keine Dauer), endet ein Paar bei höchstens 94 = „wahrscheinlich“.

**Blocker** (`blockers`) schließen eine automatische Zusammenführung aus, auch wenn der Wert
hoch ist: `platform_conflict`, `artist_similar_name`, `version_one_sided`,
`version_remixer_unknown`, `version_remaster`, `isrc_metadata_conflict`.

## Konservative Regeln für automatische Zusammenführungen

1. Nur Paare der Stufe `certain` ohne Blocker.
2. Unmittelbar vor dem Zusammenführen wird das Paar mit den aktuellen Daten neu bewertet;
   hat sich etwas geändert, wird nur der Vorschlag aktualisiert.
3. Abgelehnte und zurückgestellte Paare werden nie automatisch zusammengeführt.
4. Automatisch läuft nur auf Knopfdruck („Sichere zusammenführen“) – nicht beim Scan.
5. Behalten wird der Track mit mehr bestätigten Quellen, dann mehr Playlist-Einträgen, dann
   der ältere.
6. Jede Zusammenführung steht im Verlauf und ist rückgängig zu machen.

## Vorschläge (`match_candidates`)

`POST /api/v1/matching/scan` (oder Job `matching.scan`) vergleicht alle Tracks:

- Kandidatenpaare kommen aus dem Blocking: gleiche ISRC, gleicher normalisierter Titel oder
  gleicher Artist mit gleichem Titelanfang. Gruppen über 200 Tracks („Intro“) werden
  übersprungen. 10 000 Tracks brauchen rund zwei Sekunden.
- Neue Paare ab `review` werden als `open` gespeichert; offene und zurückgestellte Paare
  werden neu bewertet (Status bleibt) oder entfernt, wenn sie nicht mehr ähnlich genug sind.
- **Abgelehnte Paare bleiben abgelehnt** und werden nie erneut vorgeschlagen. Eine falsche
  Ablehnung lässt sich mit „Wieder öffnen“ korrigieren.

Status: `open` → `deferred` („Später prüfen“) / `rejected` („Ablehnen“) / zusammengeführt
(Paar verschwindet, Eintrag im Verlauf).

## Zusammenführen

Bestätigen führt den **Quelltrack** (neuerer Eintrag) in den **vorgeschlagenen Track**
(älterer) zusammen; im Dialog lässt sich die Richtung umkehren. Zwei beliebige Tracks lassen
sich unter „Manuell vergleichen“ prüfen und zusammenführen – auch gegen die Engine, dann mit
deutlicher Warnung. Als „unterschiedlich“ markierte Paare sind gesperrt, bis die Markierung
aufgehoben ist.

Was passiert (`library/merge.py`):

| Teil | Regel |
|---|---|
| Titel, Artist | bleiben unverändert (lokal führt) |
| Album, Version, Dauer, ISRC, Veröffentlichung, Notizen | nur ergänzt, wenn beim behaltenen Track leer |
| Plattform-Quellen | wandern mit; hat der behaltene Track auf der Plattform schon einen bestätigten Link, wird der übernommene zum Vorschlag (`candidate`) – nichts geht verloren |
| Tags | Vereinigung |
| Playlist-Einträge | zeigen auf den behaltenen Track, **Positionen bleiben gleich** |
| Importprotokoll | Verweise werden umgehängt |
| Ablehnungen | „B ≠ C“ wird bei „A = B“ zu „A ≠ C“ übernommen |

Alles wird im `snapshot` von `track_merges` festgehalten.

## Rückgängig machen

`POST /api/v1/matching/merges/{id}/undo` stellt den entfernten Track **mit derselben ID**
wieder her: Felder, Quellen samt Status und Verfügbarkeit, Tags, Playlist-Einträge (gleiche
Eintrags-IDs und Positionen), Importverweise und seine Vorschläge. Beim behaltenen Track
werden ergänzte Felder zurückgenommen, sofern sie seitdem nicht geändert wurden. Danach gilt
das Paar als abgelehnt, damit es nicht sofort wieder vorgeschlagen wird.

Grenzen: Wurde der behaltene Track inzwischen selbst in einen anderen zusammengeführt oder
gelöscht, antwortet die API mit `409` – erst die spätere Zusammenführung rückgängig machen.
Zwischenzeitliche Änderungen am behaltenen Track (z. B. neue Tags) bleiben erhalten.

## Tests

`backend/tests/test_matching.py` (reine Engine) enthält realistische Fälle für identische
Tracks, Schreibweisen, Remixe, Bootlegs, VIP, Live, Cover, Remaster, fehlende ISRC,
abweichende Längen und ähnliche Artist-Namen sowie eine Tabelle `MUST_NOT_AUTO_MERGE`, die
für jedes Paar in beide Richtungen prüft, dass es weder „sicher“ noch automatisch
zusammenführbar ist. `test_matching_api.py` prüft Scan, Entscheidungen, Zusammenführen,
Rückgängig, Plattformkonflikte und die Automatik gegen PostgreSQL.

Neue Regel? Erst einen Fall in `MUST_NOT_AUTO_MERGE` bzw. `SAME` ergänzen, dann die Engine
ändern und `ENGINE_VERSION` erhöhen.
