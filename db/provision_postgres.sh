#!/usr/bin/env bash
# Installs PostgreSQL (if missing) and creates the dedicated role + database for this bot.
# Idempotent: safe to run again. Writes DB_HOST/DB_PORT/DB_PASSWORD/... into .env.
# Usage: bash db/provision_postgres.sh [APP_DIR]
set -euo pipefail

APP_DIR="${1:-/opt/forexfactory-api}"
PY="$APP_DIR/.venv/bin/python"
SETTINGS="$APP_DIR/bot_settings.py"

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must run as root." >&2
  exit 1
fi

get_setting() { "$PY" "$SETTINGS" get "$1"; }

pg_admin() {
  (cd /tmp && runuser -u postgres -- psql -X -q -t -A -v ON_ERROR_STOP=1 "$@")
}

export DEBIAN_FRONTEND=noninteractive
if ! command -v psql >/dev/null 2>&1 || ! dpkg -s postgresql >/dev/null 2>&1; then
  echo "Installing PostgreSQL..."
  apt-get update -y
  apt-get install -y postgresql postgresql-contrib
else
  echo "PostgreSQL is already installed."
fi

systemctl enable --now postgresql >/dev/null 2>&1 || systemctl start postgresql || true

ready=false
for _ in $(seq 1 40); do
  if pg_admin -c "SELECT 1" >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
if [ "$ready" != "true" ]; then
  echo "PostgreSQL did not start. Check: systemctl status postgresql  /  pg_lsclusters" >&2
  exit 1
fi

DB_NAME="$(get_setting DB_NAME)"
DB_USER="$(get_setting DB_USER)"
DB_PW="$(get_setting DB_PASSWORD || true)"
PG_PORT="$(pg_admin -c 'SHOW port')"

for ident in "$DB_NAME" "$DB_USER"; do
  if ! [[ "$ident" =~ ^[a-z_][a-z0-9_]{0,62}$ ]]; then
    echo "Invalid DB_NAME/DB_USER '$ident' (use lowercase letters, digits and _)." >&2
    exit 1
  fi
done

if [ -z "$DB_PW" ]; then
  DB_PW="$("$PY" -c 'import secrets; print(secrets.token_hex(24))')"
  echo "Generated a new random database password."
fi

pg_admin -v dbuser="$DB_USER" -v dbname="$DB_NAME" -v dbpw="$DB_PW" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'dbuser', :'dbpw')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'dbuser') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'dbuser', :'dbpw') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'dbname', :'dbuser')
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'dbname') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'dbname', :'dbuser')
  WHERE (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :'dbname') = 'postgres' \gexec
SQL

export FFX_PROVISION_DB_PASSWORD="$DB_PW"
(cd "$APP_DIR" && "$PY" -c 'import os, bot_settings as c; v=c.read_env(); v["DB_PASSWORD"]=c.validate("DB_PASSWORD", os.environ["FFX_PROVISION_DB_PASSWORD"]); c.write_env(v)')
unset FFX_PROVISION_DB_PASSWORD
"$PY" "$SETTINGS" set "DB_HOST=127.0.0.1" "DB_PORT=$PG_PORT" "DB_SSLMODE=prefer" >/dev/null

echo "✓ PostgreSQL ready: database '$DB_NAME', user '$DB_USER', 127.0.0.1:$PG_PORT"
