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

## Schema (Revision `0003`)

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

Eine lokale Playlist hat pro Plattform höchstens ein Ziel (`uq (playlist_id, platform)`).
`remote_playlist_id` ist `NULL`, bis die Plattform-Playlist angelegt wurde.
`enabled`, `last_synced_at`, `last_error` für Steuerung und Anzeige. Noch ohne API.

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

## Geplante Tabellen

Werden erst angelegt, wenn die zugehörige Funktion gebaut wird (siehe ROADMAP):

| Tabelle | Zweck | Wichtigste Spalten |
|---|---|---|
| `connector_accounts` | verbundenes Plattform-Konto je Nutzer | `platform`, `owner_subject`, `display_name`, verschlüsselte `access_token`/`refresh_token`, `expires_at`, `scopes`, `status` |
| `sync_runs` | Historie je Synchronisation einer Playlist | `playlist_target_id`, `job_id`, `started_at`, `finished_at`, `status`, `added`, `removed`, `unresolved`, `error` |

Tokens in `connector_accounts` werden mit einem Schlüssel aus der Umgebung verschlüsselt
und nie im Klartext gespeichert oder geloggt.

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
- Revisionen fortlaufend benennen (`0003_…`): `alembic revision --autogenerate --rev-id 0003 -m "…"`.
- Migrationen müssen auf einer Datenbank mit Bestandsdaten funktionieren und eine
  sinnvolle `downgrade()` haben. Beispiel: `0002` überführt `platform_links` mit allen
  Daten nach `track_sources` und normalisiert bestehende Playlist-Positionen.

Im Betrieb migriert `musichub-serve` beim Start automatisch (Advisory-Lock, idempotent).
Manuell: `docker compose exec backend musichub-migrate`.
