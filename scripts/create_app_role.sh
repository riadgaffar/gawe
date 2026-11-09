#!/usr/bin/env bash
# Turns on login for the limited `gawe_app` role (created by migration 0004) with a password from
# GAWE_APP_DB_PASSWORD. If that is not set in .env, a random one is generated and appended there.
# Safe to run repeatedly: it sets the password again to the same value.
#
#   make app-role
#
# PSQL controls how psql is invoked (default: docker compose exec -T postgres psql -U $GAWE_DB_USER).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
PSQL=${PSQL:-"docker compose exec -T postgres psql -U ${GAWE_DB_USER:-gawe}"}

if [ -z "${GAWE_APP_DB_PASSWORD:-}" ]; then
    GAWE_APP_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
    printf '\nGAWE_APP_DB_PASSWORD=%s\n' "$GAWE_APP_DB_PASSWORD" >> .env
    echo "generated GAWE_APP_DB_PASSWORD and added it to .env"
fi

# The password reaches psql as a variable and is quoted by psql itself (:'pw').
$PSQL -d "${GAWE_DB_NAME:-gawe}" -v ON_ERROR_STOP=1 -q -v pw="$GAWE_APP_DB_PASSWORD" <<'SQL'
ALTER ROLE gawe_app LOGIN PASSWORD :'pw';
SQL
echo "gawe_app can now log in; restart the API so it connects as that role"
