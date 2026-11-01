#!/usr/bin/env sh
# MusicHub – eigene Datenbankrolle für Backend und Worker (ohne Superuser-Rechte).
#
# Das offizielle postgres-Image legt POSTGRES_USER als Superuser an. Dieses Skript legt
# zusätzlich die Rolle MUSICHUB_DB_USER an (bzw. setzt ihr Passwort neu), überträgt ihr die
# Datenbank, das Schema public und alle Tabellen. Danach verbindet sich die Anwendung mit
# dieser Rolle; POSTGRES_USER bleibt nur für Wartung (Backup, Restore, Upgrades).
#
#   1. In .env setzen:  MUSICHUB_DB_USER=musichub_app
#                       MUSICHUB_DB_PASSWORD=<openssl rand -hex 24>
#   2. scripts/create-app-role.sh
#   3. docker compose up -d      (Backend und Worker verbinden sich neu)
#
# Mehrfach ausführbar. Details: docs/SECURITY.md

set -eu
COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$COMPOSE_DIR"

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
APP_USER="$(env_value MUSICHUB_DB_USER)"
APP_PASSWORD="$(env_value MUSICHUB_DB_PASSWORD)"

if [ -z "$APP_USER" ] || [ -z "$APP_PASSWORD" ]; then
  echo "Fehler: MUSICHUB_DB_USER und MUSICHUB_DB_PASSWORD in .env setzen." >&2
  exit 1
fi
# Werte landen in SQL und in einer URL: nur einfache Zeichen zulassen
echo "$APP_USER" | grep -Eq '^[a-z_][a-z0-9_]{0,62}$' || {
  echo "Fehler: MUSICHUB_DB_USER nur aus a-z, 0-9 und _ (beginnend mit Buchstabe)." >&2; exit 1; }
echo "$APP_PASSWORD" | grep -Eq '^[A-Za-z0-9]{16,128}$' || {
  echo "Fehler: MUSICHUB_DB_PASSWORD mind. 16 Zeichen, nur Buchstaben und Ziffern." >&2; exit 1; }

docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER' AND rolsuper) THEN
    RAISE EXCEPTION 'Rolle $APP_USER ist Superuser – bitte einen anderen Namen wählen';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER') THEN
    CREATE ROLE "$APP_USER" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  EXECUTE format('ALTER ROLE %I PASSWORD %L', '$APP_USER', '$APP_PASSWORD');
  EXECUTE format('ALTER DATABASE %I OWNER TO %I', current_database(), '$APP_USER');
  EXECUTE format('ALTER SCHEMA public OWNER TO %I', '$APP_USER');
END
\$\$;
DO \$\$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS name, c.relkind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a', 'i'))
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO %I',
      CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                     WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
      r.name, '$APP_USER');
  END LOOP;
END
\$\$;
SQL
echo "Rolle $APP_USER eingerichtet. Jetzt: docker compose up -d"
