#!/bin/sh
set -eu

if [ -z "${REELOOM_APP_PASSWORD:-}" ]; then
    echo "REELOOM_APP_PASSWORD is required" >&2
    exit 1
fi

psql --set=ON_ERROR_STOP=1 \
    --set=app_password="$REELOOM_APP_PASSWORD" \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<'SQL'
SELECT format(
    'CREATE ROLE reeloom_app LOGIN PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'reeloom_app'
)
\gexec
GRANT CONNECT ON DATABASE reeloom TO reeloom_app;
SQL
