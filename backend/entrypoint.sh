#!/bin/bash
set -e

# Only the API container should run migrations; celery workers start in parallel and would race.
if [ "${SKIP_DB_MIGRATE:-}" != "1" ]; then
  echo "=== Running Alembic migrations ==="
  alembic upgrade head
else
  echo "=== Skipping Alembic (SKIP_DB_MIGRATE=1) ==="
fi

echo "=== Starting application ==="
exec "$@"
