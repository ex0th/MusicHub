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

## Schema (Revision `0001`)

```
tracks 1───n platform_links
   │
   │ (RESTRICT)
   n
playlist_items n───1 playlists 1───n playlist_targets

jobs (eigenständig)
```

### `tracks` – die lokale Sammlung

| Spalte | Typ | Hinweis |
|---|---|---|
| `id` | uuid | PK |
| `title`, `artist` | varchar(500) | Pflicht. `artist` ist vorerst die Anzeigeform („A, B feat. C“) |
| `album` | varchar(500) | optional |
| `duration_ms` | int | optional, `> 0` |
| `isrc` | varchar(12) | optional, Format `^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$`, indiziert (nicht eindeutig) |
| `notes` | text | optional |

### `platform_links` – Zuordnung Track ↔ Plattform-Track

| Spalte | Typ | Hinweis |
|---|---|---|
| `track_id` | uuid | FK → `tracks`, `ON DELETE CASCADE` |
| `platform` | varchar(32) | `spotify` \| `soundcloud` \| `youtube` |
| `external_id` | varchar(255) | ID auf der Plattform |
| `url` | varchar(1000) | optional |
| `status` | varchar(16) | `linked` \| `candidate` \| `rejected` \| `unavailable` |
| `match_method` | varchar(16) | `manual` \| `isrc` \| `fuzzy` \| `import` |
| `match_score` | float | 0…1, optional |
| `last_checked_at` | timestamptz | letzte Verfügbarkeitsprüfung |

Regeln (über partielle Unique-Indizes erzwungen):
- pro Track und Plattform höchstens **ein** `linked`-Eintrag,
- ein Plattform-Track ist höchstens **einem** lokalen Track als `linked` zugeordnet,
- beliebig viele `candidate`/`rejected`-Einträge (abgelehnte Vorschläge werden gemerkt).

### `playlists`, `playlist_items`

`playlist_items.position` (≥ 0) bestimmt die Reihenfolge; Duplikate eines Tracks sind
erlaubt (wie auf den Plattformen). Index `(playlist_id, position)`.

### `playlist_targets` – Sync-Ziele

Eine lokale Playlist hat pro Plattform höchstens ein Ziel (`uq (playlist_id, platform)`).
`remote_playlist_id` ist `NULL`, bis die Plattform-Playlist angelegt wurde.
`enabled`, `last_synced_at`, `last_error` für Steuerung und Anzeige.

### `jobs` – Hintergrundjobs

`kind`, `payload` (jsonb), `status` (`queued` | `running` | `succeeded` | `failed` | `dead`),
`attempts`/`max_attempts`, `run_after`, `locked_at`/`locked_by`, `last_error`, `finished_at`.
Index `(status, run_after)` für das Abholen.

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
- Revisionen fortlaufend benennen (`0002_…`), `revision`-ID entsprechend setzen.
- Migrationen müssen auf einer Datenbank mit Bestandsdaten funktionieren und eine
  sinnvolle `downgrade()` haben.

Im Betrieb migriert `musichub-serve` beim Start automatisch (Advisory-Lock, idempotent).
Manuell: `docker compose exec backend musichub-migrate`.
