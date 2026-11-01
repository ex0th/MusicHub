#!/usr/bin/env sh
# MusicHub – Datenbank aus einer Sicherung wiederherstellen.
#
#   scripts/restore.sh backups/musichub-20261101T020000Z.dump          # fragt nach
#   scripts/restore.sh --yes backups/musichub-20261101T020000Z.dump    # ohne Rückfrage
#
# Ablauf: Prüfsumme prüfen (falls .sha256 vorhanden) → Sicherheitskopie des aktuellen
# Stands → Backend, Worker und Frontend stoppen → Datenbank leeren und neu anlegen →
# pg_restore → Dienste starten (das Backend migriert ggf. auf den Stand des Images).
#
# Wichtig: MUSICHUB_TOKEN_ENCRYPTION_KEY muss derselbe sein wie beim Erstellen der
# Sicherung, sonst sind verbundene Plattform-Konten neu zu verbinden. Details: docs/BACKUP.md

set -eu
umask 077

YES=0
if [ "${1:-}" = "--yes" ]; then YES=1; shift; fi
DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "Aufruf: $0 [--yes] <datei.dump>" >&2
  exit 2
fi
DUMP="$(cd "$(dirname "$DUMP")" && pwd)/$(basename "$DUMP")"
COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$COMPOSE_DIR"
compose() { docker compose "$@"; }

SUMS="${DUMP%.dump}.sha256"
if [ -f "$SUMS" ]; then
  expected="$(grep " $(basename "$DUMP")\$" "$SUMS" | cut -d' ' -f1)"
  actual="$(sha256sum "$DUMP" | cut -d' ' -f1)"
  if [ -n "$expected" ] && [ "$expected" != "$actual" ]; then
    echo "Fehler: Prüfsumme stimmt nicht – Sicherung beschädigt?" >&2
    exit 1
  fi
  echo "Prüfsumme in Ordnung."
fi

compose up -d db >/dev/null
for _ in $(seq 1 30); do
  compose exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d postgres' >/dev/null 2>&1 && break
  sleep 2
done
compose exec -T db pg_restore --list < "$DUMP" > /dev/null || {
  echo "Fehler: $DUMP ist keine lesbare pg_dump-Sicherung (Custom-Format)." >&2
  exit 1
}

if [ "$YES" -ne 1 ]; then
  printf "Die aktuelle Datenbank wird durch %s ersetzt. Fortfahren? [ja/N] " "$(basename "$DUMP")"
  read -r answer
  [ "$answer" = "ja" ] || { echo "Abgebrochen."; exit 1; }
fi

echo "Sicherheitskopie des aktuellen Stands …"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backups}" KEEP_DAYS=0 "$COMPOSE_DIR/scripts/backup.sh"

echo "Stoppe Backend, Worker und Frontend …"
compose stop frontend backend worker

echo "Lege Datenbank neu an …"
compose exec -T db sh -c '
  set -e
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -q \
    -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\" WITH (FORCE)" \
    -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\""'

echo "Stelle wieder her …"
compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --role="$POSTGRES_USER" --exit-on-error --single-transaction' \
  < "$DUMP"

# Eigene Anwendungsrolle (falls eingerichtet) wieder zum Besitzer machen
if [ -f .env ] && grep -q '^MUSICHUB_DB_USER=.' .env; then
  echo "Übertrage Besitz an die Anwendungsrolle …"
  "$COMPOSE_DIR/scripts/create-app-role.sh"
fi

echo "Starte Dienste …"
compose up -d
echo "Fertig. Status prüfen: docker compose ps  sowie  /api/health/ready (Port MUSICHUB_HTTP_PORT)"
