# Synchronisierung

Lokale MusicHub-Playlists werden auf ausgewählte Playlists bei **Spotify** und **YouTube**
übertragen. Die zentrale Datenbank bleibt die führende Instanz.

- **Richtung:** ausschließlich MusicHub → Plattform. Änderungen auf der Plattform löschen
  oder verändern in MusicHub nie etwas.
- **Ziele:** Jede Playlist kann mehrere Ziele haben (auch mehrere je Plattform). Eine
  Plattform-Playlist kann nur Ziel *einer* lokalen Playlist sein.
- **SoundCloud** ist kein Ziel: Schreibzugriff setzt eine eigene API-App voraus (laut
  SoundCloud nur mit Artist Pro) und ist nicht gegen eine echte API geprüft. Die Oberfläche
  bietet SoundCloud deshalb nicht an; die API antwortet mit `422`.

## Freigaben – bevor überhaupt geschrieben wird

Alle vier Bedingungen müssen erfüllt sein. Fehlt eine, ist nur die **Vorschau** möglich.

1. **Konfiguration:** `MUSICHUB_SYNC_WRITE_PLATFORMS=spotify` (bzw. `spotify,youtube`) in
   `.env`. Standard ist leer = nichts wird geschrieben.
2. **Schreibrechte des Kontos:** Auf *Plattformen* → „Schreibzugriff erteilen“ neu
   anmelden. Angefragt werden nur Playlist-Rechte:
   - Spotify: `playlist-modify-private`, `playlist-modify-public` (zusätzlich zu den
     Lese-Scopes) – nie Bibliothek, Likes oder Follows.
   - YouTube: `https://www.googleapis.com/auth/youtube` (Playlists anlegen/ändern verlangt
     laut API diesen Scope oder `youtube.force-ssl`).
3. **Ziel aktiviert.**
4. **Bestätigte Vorschau** vor dem ersten schreibenden Sync eines Ziels (höchstens eine
   Stunde alt, gleicher Modus). Danach sind manuelle und geplante Läufe ohne erneute
   Vorschau möglich – außer beim Wechsel auf „vollständig abgleichen“.

## Einstellungen je Ziel

| Einstellung | Werte |
|---|---|
| Zielplattform | Spotify, YouTube |
| Zielplaylist | **neue Playlist anlegen** (privat, „von MusicHub verwaltet“) oder vorhandene eigene/kollaborative Playlist |
| Aktiviert | ja/nein |
| Modus | **Nur neue Tracks hinzufügen** (Standard) oder **Änderungen vollständig abgleichen** |
| Zeitplan | nur manuell, stündlich, alle 6 h, täglich, wöchentlich (mindestens 60 min) |
| Vollständig abgleichen erlaubt | nur bei von MusicHub angelegten Playlists, ausdrückliche Freigabe |

„Vollständig abgleichen“ ist **nur** für von MusicHub angelegte (`managed`) und ausdrücklich
freigegebene (`mirror_allowed`) Ziele möglich – geprüft in der API und zusätzlich als
CHECK-Constraint in der Datenbank. Vorhandene Playlists können nur ergänzt werden.

## Modi

**Nur neue Tracks hinzufügen** (`add_only`)
- Fehlende Tracks werden ans Ende angehängt; nichts wird entfernt oder umsortiert.
- Schon vorhandene Tracks werden nicht doppelt hinzugefügt; doppelte lokale Einträge nur
  einmal übertragen.
- Hat jemand einen von MusicHub hinzugefügten Track auf der Plattform entfernt, wird er
  **nicht erneut** hinzugefügt (auch nicht in späteren Läufen).

**Änderungen vollständig abgleichen** (`mirror`)
- Die Zielplaylist bekommt exakt die lokale Reihenfolge, inklusive doppelter Einträge.
- Spotify: Inhalt wird in einem Schritt ersetzt (`PUT …/items`, je 100 Einträge).
  YouTube: einzelne Löschungen, Einfügungen und Verschiebungen (je 50 Kontingent-Einheiten).
- Tracks, die nicht von MusicHub stammen, werden nur mit **ausdrücklicher Zustimmung** zu
  einer gesehenen Vorschau entfernt; dasselbe gilt für das erneute Hinzufügen extern
  entfernter Tracks. Ohne Zustimmung endet der Lauf mit Status `conflict`, nichts wird
  verändert. Geplante Läufe stimmen nie zu.

## Ablauf eines Laufs (Job `sync.run`)

1. **Lokale Playlist** in Reihenfolge laden.
2. **Tracks auf der Zielplattform bestimmen:**
   - bestätigter, nicht als unverfügbar markierter Link → wird übertragen;
   - Link als unverfügbar markiert → `unavailable`;
   - ohne Link: bisheriges Suchergebnis (`platform_lookups`), sonst Suche auf der Plattform
     (Spotify: erst ISRC, dann Titel/Artist; YouTube: `search.list`, eigenes Tageslimit).
     Nur **sichere** Treffer der Matching-Engine werden verwendet und beim schreibenden Sync
     lokal verknüpft; ähnliche Treffer bleiben Vorschlag (`candidate`). Höchstens
     `MUSICHUB_SYNC_LOOKUP_LIMIT` Suchen je Lauf.
3. **Zielplaylist einlesen** (fehlt sie auf der Plattform → `failed`).
4. **Differenzen berechnen** – reine Funktion `sync/engine.plan_sync`.
5. **Vorschau speichern.** Ein Dry-Run endet hier (Status `preview`).
6. **Ausführen** nach den Sicherheitsprüfungen: Freigaben, Zustimmung bei externen
   Änderungen, und – wenn sich der Lauf auf eine Vorschau beruft – keine Entfernungen, die
   in der Vorschau nicht standen (sonst `conflict` „seit der Vorschau geändert“).
7. **Ergebnis prüfen und dokumentieren:** Zielplaylist erneut lesen, mit dem Soll
   vergleichen (`partial`, wenn etwas fehlt), Historie und Ziel aktualisieren.

## Fehlende Tracks

Ein Track, der auf der Zielplattform nicht gefunden wird, bleibt in MusicHub vollständig
erhalten und steht im Bericht und unter *Nicht übertragbare Tracks*:

| Status | Bedeutung |
|---|---|
| `unchecked` | noch nicht geprüft (z. B. Suchlimit des Laufs erreicht) |
| `not_found` | gesucht, kein passender Treffer |
| `api_error` | Suche fehlgeschlagen (Fehler, Kontingent) – wird beim nächsten Lauf automatisch wiederholt |
| `candidate` | nur ähnlicher Treffer – Link in der Übersicht, wird nicht automatisch verwendet |
| `unavailable` | verknüpft, aber auf der Plattform als nicht verfügbar markiert |

Angezeigt werden bekannte Links auf anderen Plattformen. „Erneut prüfen“ verwirft die
Suchergebnisse; die nächste Vorschau sucht neu.

## Externe Änderungen erkennen

Nach jedem erfolgreichen Lauf speichert das Ziel `known_remote_items`: die Einträge der
Zielplaylist, für die MusicHub verantwortlich ist (Schnittmenge aus Plattform und lokaler
Playlist, plus bewusst nicht erneut hinzugefügte Tracks). Beim nächsten Lauf ergibt der
Vergleich:

- **extern hinzugefügt** = auf der Plattform, aber nicht bekannt (bleibt bei `add_only`,
  braucht bei `mirror` Zustimmung zum Entfernen),
- **extern entfernt** = bekannt, aber nicht mehr auf der Plattform (wird bei `add_only` nicht
  erneut hinzugefügt, bei `mirror` nur mit Zustimmung).

Beim ersten Lauf gilt alles Vorhandene als extern.

## Idempotenz und Fehlerverhalten

- Jeder Lauf plant anhand des **tatsächlichen** Stands der Plattform neu. Ein zweiter Lauf
  nach einem erfolgreichen ist leer (`no_changes`) und schreibt nichts.
- Je Ziel läuft höchstens ein Lauf gleichzeitig.
- Eine neu angelegte Zielplaylist wird sofort (eigene Transaktion) am Ziel gespeichert – ein
  späterer Fehler führt nie zu einer zweiten Playlist.
- **Schreibaufrufe werden nie automatisch wiederholt** (Zeitüberschreitung, 5xx, 4xx): Der
  Lauf endet sofort mit `failed`, ohne weitere Änderungen; der Job wird nicht wiederholt.
- **Rate-Limit/Kontingent:** Kurze `Retry-After` (≤ 5 s) wartet der Client ab. Längere
  Wartezeiten: Lauf wartet (`queued`), Job startet nach der Wartezeit neu und plant neu
  (höchstens 3 Versuche). YouTube-Kontingent: nach Mitternacht pazifischer Zeit.
- Es gibt keine Löschaktion außerhalb von `mirror`; Löschungen betreffen nur Einträge, die
  im Plan stehen.

## Zeitplan

Der Worker prüft alle `MUSICHUB_SYNC_SCHEDULER_INTERVAL_SECONDS` (Standard 60 s) fällige
Ziele (`SKIP LOCKED`, mehrere Worker möglich). Geplant wird nur, wenn das Ziel aktiviert und
bestätigt ist, alle Freigaben vorliegen und kein Lauf aktiv ist.

## Historie (`sync_runs`)

Je Lauf: Start-/Endzeit, Plattform, Playlist (Name wird mitgeschrieben), Zielplaylist,
Auslöser (`preview` | `manual` | `scheduled`), Modus, Dry-Run, Zustimmung, Anzahl
hinzugefügt/entfernt/übersprungen/nicht zugeordnet, Fehler, Status (`queued` | `running` |
`preview` | `succeeded` | `no_changes` | `partial` | `conflict` | `blocked` | `failed`)
und der vollständige Plan (Vorschau). Die Historie bleibt erhalten, wenn ein Ziel oder die
Playlist gelöscht wird.

## Oberfläche

Playlist → **Synchronisation** (`/sync/?id=…`): Ziele mit Einstellungen, Status, nächstem
geplanten Lauf und Schreib-Hindernissen; „Vorschau erstellen“, „Vorschau bestätigen und
synchronisieren“, „Jetzt synchronisieren“; Vorschau mit Hinzufügen/Entfernen, externen
Änderungen, Kontingentbedarf (YouTube) und nicht übertragbaren Tracks; Liste nicht
übertragbarer Tracks mit „Erneut prüfen“; Historie mit Details.

## Kontingente und Grenzen

- Spotify: höchstens 100 Einträge je Schreibaufruf; im Development Mode nur eigene oder
  kollaborative Playlists; Suche höchstens 10 Treffer je Anfrage.
- YouTube: `playlists.insert`, `playlistItems.insert/update/delete` kosten je 50 Einheiten
  (10 000 pro Tag → etwa 200 Änderungen). Die Vorschau zeigt den Bedarf. `search.list` hat
  ein eigenes Limit von 100 Aufrufen pro Tag.
- Wird ein Ziel entfernt, bleibt die Plattform-Playlist unverändert bestehen.

## Tests

`test_sync_engine.py` (reine Planung: Modi, Duplikate, externe Änderungen, Idempotenz,
Verschiebungen), `test_sync_api.py` (Freigaben, Dry-Run, Bestätigung, fehlende Tracks inkl.
API-Fehler bei der Suche, add_only in vorhandene Playlists, Duplikate, mirror mit Konflikt
und Zustimmung, Änderung nach der Vorschau, Rate-Limit mit genau einem späteren Versuch,
Schreibfehler ohne Wiederholung, Zeitplan, YouTube mit Kontingent und Verschiebungen) und
`test_connectors.py` (Schreib-Scopes, Aufteilung in 100er-Blöcke, private neue Playlists,
keine Wiederholung bei Zeitüberschreitung).
