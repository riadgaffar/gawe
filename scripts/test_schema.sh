#!/usr/bin/env bash
# Applies migrations/sql/*.sql to a throwaway database, then runs tests/sql/*.sql against it.
#
# PSQL controls how psql is invoked. Examples:
#   PSQL="docker compose exec -T postgres psql -U gawe" scripts/test_schema.sh   # compose DB
#   PSQL="psql -h /tmp/sock -U me" scripts/test_schema.sh                         # local server
set -uo pipefail
cd "$(dirname "$0")/.."

PSQL=${PSQL:-psql}
DB="gawe_schema_test_$$"

cleanup() { $PSQL -d postgres -qc "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if ! $PSQL -d postgres -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $DB"; then
    echo "could not create test database (is Postgres running? is PSQL set correctly?)" >&2
    exit 2
fi

for f in migrations/sql/*.sql; do
    echo "applying $f"
    if ! out=$($PSQL -d "$DB" -v ON_ERROR_STOP=1 -q < "$f" 2>&1); then
        echo "$out" >&2
        echo "migration failed: $f" >&2
        exit 1
    fi
done

status=0
passes=0
for f in tests/sql/*.sql; do
    echo "running $f"
    out=$($PSQL -d "$DB" -v ON_ERROR_STOP=1 -q < "$f" 2>&1) || status=1
    file_passes=$(printf '%s\n' "$out" | grep -c 'PASS:' || true)
    passes=$((passes + file_passes))
    if [ "$status" -ne 0 ]; then
        printf '%s\n' "$out" | grep -E 'PASS:|FAIL:|ERROR' >&2
        break
    fi
done

if [ "$status" -ne 0 ]; then
    echo "SCHEMA TESTS FAILED after $passes passing checks" >&2
    exit 1
fi
echo "schema tests passed: $passes checks"
