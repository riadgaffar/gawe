#!/usr/bin/env bash
# The packaged demo on Postgres (M29): `make demo DEMO_STORE=postgres`.
#
# Uses its own database, gawe_demo, in the compose Postgres, never the development database
# named in .env. Creates it if missing, migrates it, adds the demo companies' tenants and users,
# then starts the demo. Runs kept here survive a restart; `make demo-reset` drops the database.
#
# PSQL controls how psql is invoked (default: docker compose exec -T postgres psql -U $GAWE_DB_USER).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DEMO_DB=gawe_demo
# Everything below reads the database name from these; nothing may point elsewhere.
unset GAWE_DATABASE_URL GAWE_APP_DATABASE_URL
export GAWE_DB_NAME=$DEMO_DB
PSQL=${PSQL:-"docker compose exec -T postgres psql -U ${GAWE_DB_USER:-gawe}"}

# Wait for Postgres (just started by `make up`).
for _ in $(seq 1 30); do
    $PSQL -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 && break
    sleep 1
done

if [ "$($PSQL -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DEMO_DB'")" != "1" ]; then
    $PSQL -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE $DEMO_DB"
    echo "created database $DEMO_DB"
fi

# Prove the target before migrating or seeding: both must name gawe_demo.
target=$(python -c 'from sqlalchemy.engine import make_url
from gawe.db.session import database_url
print(make_url(database_url()).database)')
if [ "$target" != "$DEMO_DB" ]; then
    echo "refusing: migrations would run against '$target', not $DEMO_DB" >&2
    exit 1
fi
alembic upgrade head

guard="DO \$\$ BEGIN IF current_database() <> '$DEMO_DB' THEN
    RAISE EXCEPTION 'refusing: not connected to $DEMO_DB'; END IF; END \$\$;"
{ printf '%s\n' "$guard"; python -m gawe.demo seed-sql; } | $PSQL -d "$DEMO_DB" -v ON_ERROR_STOP=1 -q
echo "seeded the demo companies in $DEMO_DB"

exec python -m gawe.demo run --store postgres
