# Datenbank

PostgreSQL 17, Zugriff über SQLAlchemy 2 (synchron, psycopg 3), Migrationen mit Alembic.

## Konventionen

- Primärschlüssel: `id UUID` (in Python erzeugt, `uuid4`).
- `created_at` / `updated_at` als `timestamptz`, Standard `now()`.
- Aufzählungen (Plattform, Status …) als `varchar` mit **CHECK-Constraint**, nicht als
  PostgreSQL-ENUM. Neue Werte brauchen nur eine kleine Migration, die den Constraint ersetzt.
  Quelle der Werte: `musichub/domain/types.py`.
- Constraint-Namen folgen einer festen Konvention (`db/base.py`): `pk_`, `fk_`, `uq_`,
  `ck_`, `ix_` – Autogenerate erzeugt dadurch stabile Migrationen.
- Fremdschlüssel löschen nur dort kaskadierend, wo das Kind ohne Eltern sinnlos ist
  (Links eines Tracks, Einträge einer Playlist). Ein Track in einer Playlist ist mit
  `RESTRICT` geschützt.

## Schema (Revision `0006`)

```
                tags
                 │ n
                 │
            track_tags
                 │ n
                 │
tracks 1───n track_sources
   │
   │ (RESTRICT)
   n
playlist_items n───1 playlists 1───n playlist_targets

jobs (eigenständig)
import_runs 1───n import_items ──(SET NULL)──> tracks
match_candidates (track_a_id, track_b_id) ──(CASCADE)──> tracks
track_merges (kept_track_id, removed_track_id – ohne Fremdschlüssel)
connector_accounts (eine Zeile je Plattform)    oauth_states (laufende Anmeldungen)
remote_playlists ──(SET NULL)──> playlists, import_runs
sync_runs ──(SET NULL)──> playlist_targets, playlists
platform_lookups ──(CASCADE)──> tracks
```

### `tracks` – die lokale Sammlung

| Spalte | Typ | Hinweis |
|---|---|---|
| `id` | uuid | PK |
| `title`, `artist` | varchar(500) | Pflicht. `artist` ist vorerst die Anzeigeform („A, B feat. C“) |
| `album` | varchar(500) | optional |
| `version` | varchar(200) | Version/Remix („Extended Mix“, „Live“); leer = Originalversion |
| `duration_ms` | int | optional, `> 0` |
| `isrc` | varchar(12) | optional, Format `^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$`, indiziert (nicht eindeutig) |
| `release_date` | date | optional |
| `notes` | text | optional |
| `created_at`, `updated_at` | timestamptz | `updated_at` ändert sich auch bei neuen Quellen/Tags |

Index `(artist, title)` für die Standardsortierung.

### `track_sources` – der Track auf einer Plattform

Ein Track kann beliebig viele Quellen haben, aber pro Plattform höchstens eine bestätigte.

| Spalte | Typ | Hinweis |
|---|---|---|
| `track_id` | uuid | FK → `tracks`, `ON DELETE CASCADE` |
| `platform` | varchar(32) | `spotify` \| `soundcloud` \| `youtube` |
| `external_id` | varchar(255) | Spotify-Track-ID, YouTube-Video-ID bzw. SoundCloud-Permalink `nutzer/track` |
| `url` | varchar(1000) | Link zur Plattform (kanonisch erzeugt, wenn nicht angegeben) |
| `link_status` | varchar(16) | `linked` (bestätigt) \| `candidate` (Vorschlag) \| `rejected` (abgelehnt, wird nicht erneut vorgeschlagen) |
| `availability` | varchar(16) | `unknown` \| `available` \| `unavailable` |
| `match_method` | varchar(16) | `manual` \| `isrc` \| `fuzzy` \| `import` |
| `match_score` | float | 0…1, optional |
| `last_checked_at` | timestamptz | Zeitpunkt der letzten Verfügbarkeitsprüfung |

Verknüpfungsstatus und Verfügbarkeit sind getrennt: Eine bestätigte Quelle kann später
`unavailable` werden (Track auf der Plattform gelöscht), ohne die Zuordnung zu verlieren.

Regeln (partielle Unique-Indizes auf `link_status = 'linked'`):
- pro Track und Plattform höchstens **eine** bestätigte Quelle (eindeutiges Sync-Ziel),
- ein Plattform-Track ist höchstens **einem** lokalen Track als bestätigt zugeordnet,
- dieselbe Kombination Track/Plattform/ID gibt es nur einmal (`uq_track_sources_track_id`).

**„Verfügbar auf Plattform X“** heißt überall (API-Filter, Statistik, UI): es gibt eine
Quelle mit `link_status = 'linked'` und `availability <> 'unavailable'`
(`library/queries.py::source_is_available`).

### `tags`, `track_tags`

| Spalte | Typ | Hinweis |
|---|---|---|
| `name` | varchar(100) | Pflicht |
| `category` | varchar(50) | Standard „Allgemein“; frei wählbar („Genre“, „Stimmung“ …) |

Eindeutig ist `(lower(category), lower(name))` – „House“ und „house“ in „Genre“ sind ein Duplikat.
`track_tags (track_id, tag_id)` ist die n:m-Zuordnung, beide Seiten `ON DELETE CASCADE`:
Löschen eines Tags entfernt nur die Zuordnung, nie Tracks.

### `playlists`, `playlist_items`

`playlist_items.position` ist pro Playlist **lückenlos 0..n-1**. `(playlist_id, position)`
ist eindeutig, die Prüfung erfolgt aber erst beim Commit (`DEFERRABLE INITIALLY DEFERRED`).
Dadurch lassen sich Einträge mit einem einzigen `UPDATE` verschieben. Jede Änderung sperrt
vorher die Playlist-Zeile (`SELECT … FOR UPDATE`), siehe `library/playlists.py`.
Duplikate eines Tracks in einer Playlist sind erlaubt (wie auf den Plattformen).
Ein Track in einer Playlist ist per `RESTRICT` vor direktem Löschen geschützt; die API
entfernt ihn mit `force=true` zuerst aus allen Playlists und nummeriert neu.

### `playlist_targets` – Sync-Ziele

Eine Playlist kann mehrere Ziele haben (seit `0006` auch mehrere je Plattform); eine
Plattform-Playlist ist höchstens Ziel einer lokalen Playlist (Teil-Unique-Index
`uq_playlist_targets_remote` auf `(platform, remote_playlist_id)`). `remote_playlist_id`
ist `NULL`, bis MusicHub die Playlist beim ersten Sync anlegt. Weitere Spalten:
`remote_name`, `remote_url`, `enabled`, `mode` (`add_only` | `mirror`), `managed` (von
MusicHub angelegt), `mirror_allowed`, `schedule_minutes` (≥ 60) und `next_run_at`,
`confirmed_at`/`confirmed_by` (erste Vorschau bestätigt), `known_remote_items` (jsonb,
Grundlage für externe Änderungen), `last_synced_at`, `last_status`, `last_run_id`,
`last_error`, `created_by`. CHECK `mirror_only_managed`: `mode = 'mirror'` nur mit
`managed AND mirror_allowed`. Details: [SYNC.md](SYNC.md).

### `sync_runs` – Synchronisationshistorie

Je Lauf (auch Vorschau): `target_id`/`playlist_id` (SET NULL, Namen werden
mitgeschrieben), `platform`, `remote_playlist_id`, `remote_name`, `trigger`, `status`,
`dry_run`, `mode`, `approved_external_removal`, `started_at`, `finished_at`, `added`,
`removed`, `skipped`, `unmatched`, `plan` (jsonb, Vorschau), `errors` (jsonb),
`preview_run_id`, `job_id`, `created_by`.

### `platform_lookups` – Suche nicht verknüpfter Tracks

Je `(track_id, platform)` (CASCADE): `status` (`found` | `candidate` | `not_found` |
`api_error`), gefundene `external_id`, `confidence`, `message`, `checked_at`. Kein Eintrag =
„noch nicht geprüft“.

### `jobs` – Hintergrundjobs

`kind`, `payload` (jsonb), `status` (`queued` | `running` | `succeeded` | `failed` | `dead`),
`attempts`/`max_attempts`, `run_after`, `locked_at`/`locked_by`, `last_error`, `finished_at`.
Index `(status, run_after)` für das Abholen.

### `import_runs`, `import_items` – Importprotokoll

Jeder Upload erzeugt einen `import_runs`-Eintrag – auch abgelehnte Dateien (`status = failed`,
`error`). Gespeichert werden Dateiname, Größe, SHA-256 (indiziert, erkennt wiederholte
Uploads), erkanntes `format`, `status` (`analyzed` | `committed` | `discarded` | `failed`),
`created_by` (Subjekt des Nutzers), `options` (Playlist-Entscheidungen, jsonb), `summary`
(Zähler, jsonb), `warnings`, `committed_at`. Die Datei selbst wird nicht gespeichert.

`import_items` hält je Datensatz: `seq` (Reihenfolge, eindeutig je Lauf), `row`
(Zeilennummer), `status` (`new` | `existing` | `potential_duplicate` | `file_duplicate` |
`invalid`), `decision` (`create` | `use_existing` | `skip`), Abgleich (`match_track_id`,
`match_reason`, `match_score`, `duplicate_of_seq`), den normalisierten Datensatz `data`
(jsonb, inkl. Fehler und Hinweise), `playlists` und nach dem Commit `result`
(`created` | `linked` | `skipped` | `failed`), `result_track_id`, `result_message`.
Verweise auf Tracks sind `ON DELETE SET NULL` – das Protokoll bleibt, wenn Tracks gelöscht werden.

### `match_candidates` – Matching-Vorschläge

Ein ungeordnetes Paar lokaler Tracks, gespeichert mit `track_a_id < track_b_id`
(CHECK `ordered_pair`, eindeutig je Paar, beide `ON DELETE CASCADE`). Spalten:
`confidence` (0–100), `level` (`certain` | `likely` | `review` | `different`), `reasons`
und `blockers` (jsonb, Begründung der Engine), `engine_version`, `status` (`open` |
`deferred` | `rejected`), `decided_at`, `decided_by`. Index `(status, confidence)`.
Abgelehnte Paare sind das Gedächtnis der Engine und werden bei Scans nicht verändert.

### `track_merges` – Protokoll der Zusammenführungen

`kept_track_id`, `removed_track_id` (bewusst **ohne** Fremdschlüssel, damit das Protokoll
Löschungen überdauert), Titel/Artist beider Tracks zum Anzeigen, `method` (`manual` |
`auto`), `confidence`, `level`, `status` (`merged` | `undone`), `created_by`,
`undone_at`, `undone_by` und `snapshot` (jsonb): der entfernte Track mit allen Feldern,
seine Quellen (mit Aktion `moved` | `moved_demoted` | `deleted_duplicate`), Tags,
umgehängte Playlist-Einträge und Importverweise, ergänzte Felder, gesicherte Vorschläge.
Daraus stellt `library/merge.py::undo_merge` den Track mit derselben ID wieder her.

### `connector_accounts` – verbundene Plattform-Konten

Eine Zeile je Plattform (`platform` eindeutig): `external_user_id`, `display_name`,
`scopes`, **verschlüsselt** `access_token_enc` und `refresh_token_enc` (Fernet, Schlüssel
aus `MUSICHUB_TOKEN_ENCRYPTION_KEY`), `token_expires_at`, `status` (`connected` |
`reauth_required`), `last_error`, `connected_by`, `connected_at`, `playlists_synced_at`,
`last_import_at`. Klartext-Tokens stehen nirgends in der Datenbank.

### `oauth_states` – laufende Anmeldungen

`state_hash` (SHA-256 des `state`, eindeutig), `platform`, `code_verifier_enc`
(verschlüsselter PKCE-Verifier), `created_by`, `created_at`, `expires_at` (10 Minuten).
Einträge werden beim Rückruf gelöscht (einmalig), abgelaufene beim nächsten Start.

### `remote_playlists` – Playlists der Plattformen

Je Plattform-Playlist (`platform`, `external_id` eindeutig): `name`, `owner_name`,
`description`, `track_count`, `url`, `owned`, `items_accessible` (Spotify Development Mode:
nur eigene/kollaborative), `remote_version` (snapshot_id/ETag/last_modified), `seen_at`,
`removed_at` (nicht mehr in der Liste), Importzustand `local_playlist_id` (→ `playlists`,
SET NULL), `imported_version`, `last_imported_at`, `last_import_status` (`ok` |
`unchanged` | `error`), `last_import_run_id` (→ `import_runs`, SET NULL), `last_error`.
Gleiches `imported_version` und `remote_version` = seit dem Import unverändert.

### `jobs` – Ergänzungen in `0005`

`progress` (jsonb, `{current, total, message}`, Standard `{}`) – wird während des Laufs in
eigener Transaktion geschrieben; `result` (jsonb) – Ergebnis erfolgreicher Jobs.

## Geplante Tabellen

Werden erst angelegt, wenn die zugehörige Funktion gebaut wird (siehe ROADMAP):

| Tabelle | Zweck | Wichtigste Spalten |
|---|---|---|
| – | derzeit keine | – |

## Migrationen

Alembic-Skripte liegen im Paket (`backend/src/musichub/migrations`), damit sie im Image
enthalten sind. `alembic.ini` verweist mit `script_location = musichub:migrations` darauf.

```bash
cd backend
export MUSICHUB_DATABASE_URL=postgresql+psycopg://musichub:musichub@localhost:5432/musichub

uv run alembic revision --autogenerate -m "kurze beschreibung"   # neue Migration
uv run alembic upgrade head                                     # anwenden
uv run alembic downgrade -1                                     # einen Schritt zurück
uv run alembic check                                            # Modelle == Schema?
```

Regeln:
- Jede Schemaänderung = Modelländerung **plus** Migration im selben Commit.
  Die CI prüft mit `alembic check`, dass nichts fehlt.
- Generierte Migrationen immer lesen und nachbessern (Datenmigrationen, Defaults für
  neue Pflichtspalten, CHECK-Constraints).
- Revisionen fortlaufend benennen (`0006_…`): `alembic revision --autogenerate --rev-id 0006 -m "…"`.
- Migrationen müssen auf einer Datenbank mit Bestandsdaten funktionieren und eine
  sinnvolle `downgrade()` haben. Beispiel: `0002` überführt `platform_links` mit allen
  Daten nach `track_sources` und normalisiert bestehende Playlist-Positionen.

Im Betrieb migriert `musichub-serve` beim Start automatisch (Advisory-Lock, idempotent).
Manuell: `docker compose exec backend musichub-migrate`.
