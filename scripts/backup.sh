#!/usr/bin/env sh
# MusicHub – Sicherung der Datenbank und der Konfiguration.
#
#   scripts/backup.sh                 # nach ./backups, alte Sicherungen > 14 Tage löschen
#   BACKUP_DIR=/srv/backup/musichub KEEP_DAYS=30 scripts/backup.sh
#   COMPOSE_DIR=/opt/arcane/projects/musichub scripts/backup.sh
#
# Erzeugt je Lauf:
#   musichub-<zeit>.dump        pg_dump im Custom-Format (komprimiert, für pg_restore)
#   musichub-<zeit>-config.tar.gz  .env und Compose-Dateien (enthält Geheimnisse!)
#   musichub-<zeit>.sha256      Prüfsummen
# Die Datenbank bleibt während der Sicherung in Betrieb (konsistenter Snapshot).
# Details und Wiederherstellung: docs/BACKUP.md

set -eu
umask 077

COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="musichub-$STAMP"

cd "$COMPOSE_DIR"
mkdir -p "$BACKUP_DIR"

compose() { docker compose "$@"; }

if ! compose ps --status running --services 2>/dev/null | grep -qx db; then
  echo "Fehler: Der Dienst 'db' läuft nicht (docker compose ps)." >&2
  exit 1
fi

echo "Sichere Datenbank nach $BACKUP_DIR/$NAME.dump …"
compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6 --no-owner' \
  > "$BACKUP_DIR/$NAME.dump.part"
mv "$BACKUP_DIR/$NAME.dump.part" "$BACKUP_DIR/$NAME.dump"

# Lesbarkeit prüfen: pg_restore muss das Inhaltsverzeichnis lesen können
if ! compose exec -T db pg_restore --list < "$BACKUP_DIR/$NAME.dump" > /dev/null; then
  echo "Fehler: Die Sicherung ist nicht lesbar." >&2
  exit 1
fi

echo "Sichere Konfiguration …"
set --
for f in .env compose.yaml compose.override.yaml; do
  [ -f "$f" ] && set -- "$@" "$f"
done
tar -czf "$BACKUP_DIR/$NAME-config.tar.gz" "$@"

(cd "$BACKUP_DIR" && sha256sum "$NAME.dump" "$NAME-config.tar.gz" > "$NAME.sha256")

if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'musichub-*' -mtime +"$KEEP_DAYS" -print -delete \
    | sed 's/^/Gelöscht (älter als '"$KEEP_DAYS"' Tage): /'
fi

SIZE="$(du -h "$BACKUP_DIR/$NAME.dump" | cut -f1)"
echo "Fertig: $BACKUP_DIR/$NAME.dump ($SIZE), $NAME-config.tar.gz, $NAME.sha256"
