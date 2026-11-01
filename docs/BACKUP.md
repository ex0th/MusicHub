# Backup, Wiederherstellung, Upgrade und Rollback

## Was gesichert werden muss

| Was | Wo | Warum |
|---|---|---|
| PostgreSQL-Datenbank | Volume `musichub_postgres-data` | gesamte Bibliothek, Playlists, Sync-Ziele, Historie, verschlüsselte Plattform-Tokens |
| `.env` | Projektordner | Zugangsdaten, **`MUSICHUB_TOKEN_ENCRYPTION_KEY`** |
| `compose.yaml`, `compose.override.yaml` | Projektordner | eigene Anpassungen |

Nicht nötig: Images (liegen in GHCR), Container, Logs.

> **Ohne denselben `MUSICHUB_TOKEN_ENCRYPTION_KEY` lassen sich gespeicherte Plattform-Tokens
> nicht entschlüsseln.** Die Bibliothek bleibt vollständig, aber Spotify/YouTube müssen neu
> verbunden werden. Den Schlüssel zusätzlich außerhalb der VM aufbewahren (Passwortmanager).

## Sicherung erstellen

```bash
cd /opt/musichub
scripts/backup.sh
```

Ergebnis in `./backups/` (Rechte `600`, Ordner `700`):

- `musichub-<UTC-Zeit>.dump` – `pg_dump` im Custom-Format, komprimiert. Läuft im laufenden
  Betrieb (konsistenter Snapshot), das Skript prüft anschließend mit `pg_restore --list`,
  dass die Datei lesbar ist.
- `musichub-<UTC-Zeit>-config.tar.gz` – `.env` und Compose-Dateien (**enthält Geheimnisse**)
- `musichub-<UTC-Zeit>.sha256` – Prüfsummen

Optionen: `BACKUP_DIR=/srv/backup/musichub`, `KEEP_DAYS=30` (ältere Sicherungen werden
gelöscht, `0` = nie), `COMPOSE_DIR=/pfad/zum/projekt` (z. B. Arcane-Projektordner).

### Automatisch (täglich 03:15)

```bash
sudo tee /etc/cron.d/musichub-backup >/dev/null <<'EOF'
15 3 * * * root cd /opt/musichub && BACKUP_DIR=/srv/backup/musichub scripts/backup.sh >> /var/log/musichub-backup.log 2>&1
EOF
```

### Außerhalb der VM aufbewahren

Ein Backup auf derselben VM schützt nicht vor Plattenausfall. Beispiel mit `restic`
(verschlüsselt, inkrementell) auf ein NAS oder S3-Ziel:

```bash
restic -r sftp:backup@nas:/musichub backup /srv/backup/musichub
```

Die Sicherungen enthalten Zugangsdaten – nur verschlüsselt oder auf vertrauenswürdige
Ziele kopieren.

### Zusätzlich: Export der Bibliothek

*Bibliothek → Export* (JSON/CSV) ist plattformunabhängig lesbar und eignet sich als zweite,
menschenlesbare Sicherung. Es ersetzt das Datenbank-Backup nicht (keine Sync-Ziele,
Historie, Matching-Entscheidungen, Konten).

## Wiederherstellen

```bash
scripts/restore.sh backups/musichub-20261101T021500Z.dump
```

Das Skript

1. prüft die Prüfsumme (falls `.sha256` daneben liegt) und die Lesbarkeit,
2. fragt nach (`ja`) – mit `--yes` ohne Rückfrage,
3. erstellt **vorher eine Sicherheitskopie** des aktuellen Stands,
4. stoppt Frontend, Backend und Worker,
5. löscht die Datenbank und legt sie neu an, spielt die Sicherung in **einer** Transaktion ein,
6. überträgt den Besitz an die eigene App-Rolle (falls `MUSICHUB_DB_USER` gesetzt),
7. startet alle Dienste. Ist die Sicherung älter als das Image, migriert das Backend beim
   Start automatisch auf den aktuellen Stand.

Konfiguration wiederherstellen (neue VM):

```bash
tar -xzf musichub-<zeit>-config.tar.gz -C /opt/musichub && chmod 600 /opt/musichub/.env
docker compose up -d db && scripts/restore.sh --yes musichub-<zeit>.dump
```

**Wiederherstellung regelmäßig testen** (z. B. vierteljährlich auf einer Test-VM): ein
Backup, das nie eingespielt wurde, ist keins. Die CI prüft Backup → Änderung →
Wiederherstellung bei jedem Commit gegen den echten Stack.

## Upgrade

### Automatisch (Arcane, `MUSICHUB_VERSION=latest`)

Arcane zieht neue Images und erstellt die Container neu. Das Backend migriert beim Start
(mit Sperre gegen parallele Migrationen), der Worker wartet auf das aktuelle Schema.
Voraussetzung: tägliches Backup läuft.

### Manuell (empfohlen bei Releases mit Schemaänderung)

```bash
cd /opt/musichub
scripts/backup.sh                                        # 1. Sicherung
docker compose exec -T db psql -U musichub -d musichub -tAc 'select version_num from alembic_version'
                                                         # 2. Schema-Stand notieren (z. B. 0007)
sed -i 's/^MUSICHUB_VERSION=.*/MUSICHUB_VERSION=sha-abc1234/' .env   # 3. Zielversion
docker compose pull && docker compose up -d              # 4. Update
docker compose ps && docker compose logs --since 5m backend          # 5. Kontrolle
curl -fsS http://127.0.0.1:8080/api/health/ready
```

Release-Hinweise und Migrationen stehen in den Commits (`backend/src/musichub/migrations/versions/`).

### PostgreSQL-Major-Version (z. B. 17 → 18)

Wird bewusst nicht automatisch aktualisiert (Datenformat ändert sich).

```bash
scripts/backup.sh
docker compose down
docker volume rm musichub_postgres-data          # erst NACH erfolgreichem Backup!
sed -i 's/postgres:17-alpine/postgres:18-alpine/' compose.yaml
docker compose up -d db
scripts/restore.sh --yes backups/<letzte>.dump
```

## Rollback bei fehlgeschlagenem Update

**Fall 1 – ohne Schemaänderung** (alter und neuer Stand haben dieselbe Alembic-Revision):
alte Version eintragen und neu starten.

```bash
sed -i 's/^MUSICHUB_VERSION=.*/MUSICHUB_VERSION=sha-<vorher>/' .env
docker compose up -d
```

**Fall 2 – das neue Image hat migriert.** Ein älteres Backend kennt die neue Revision nicht
und startet nicht (`Can't locate revision …` im Log). Zwei Wege:

a) **Sicherung einspielen** (immer möglich, verliert Änderungen seit dem Backup):

```bash
sed -i 's/^MUSICHUB_VERSION=.*/MUSICHUB_VERSION=sha-<vorher>/' .env
scripts/restore.sh --yes backups/<backup-vor-dem-update>.dump
```

b) **Schema mit dem neuen Image zurückmigrieren** (behält Daten, nur wenn die Migrationen
umkehrbar sind – bei MusicHub bisher alle):

```bash
docker compose stop frontend worker
docker compose run --rm --no-deps backend alembic downgrade <alte-revision>   # z. B. 0006
sed -i 's/^MUSICHUB_VERSION=.*/MUSICHUB_VERSION=sha-<vorher>/' .env
docker compose up -d
```

Beim Zurückmigrieren gehen Daten verloren, die nur in neuen Tabellen/Spalten stehen
(z. B. bei `0007` die Anmeldesitzungen – alle Nutzer melden sich neu an).

**Fall 3 – Container starten nicht, Ursache unklar:** [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
