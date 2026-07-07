#!/bin/bash
set -e

APP_USER="${APP_USER:-appuser}"
APP_GROUP="${APP_GROUP:-appuser}"

prepare_runtime_user() {
  if [ "$(id -u)" != "0" ] || [ "${RUN_AS_ROOT:-0}" = "1" ]; then
    return
  fi

  # Give the non-root runtime user access to the mounted Docker socket without
  # hard-coding host-specific group ids.
  if [ -S /var/run/docker.sock ]; then
    DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
    if ! getent group "${DOCKER_GID}" >/dev/null; then
      groupadd --gid "${DOCKER_GID}" dockerhost
    fi
    DOCKER_GROUP="$(getent group "${DOCKER_GID}" | cut -d: -f1)"
    usermod -aG "${DOCKER_GROUP}" "${APP_USER}" || true
  fi

  mkdir -p /app/backups /releases /lora-data
  chown -R "${APP_USER}:${APP_GROUP}" /app/backups /releases /lora-data 2>/dev/null || true
  if [ "${CHOWN_COMFYUI_LORAS:-0}" = "1" ] && [ -d /comfyui-loras ]; then
    chown -R "${APP_USER}:${APP_GROUP}" /comfyui-loras 2>/dev/null || true
  fi
}

prepare_runtime_user

# Only the API container should run migrations; celery workers start in parallel and would race.
if [ "${SKIP_DB_MIGRATE:-}" != "1" ]; then
  echo "=== Running Alembic migrations ==="
  alembic upgrade heads
else
  echo "=== Skipping Alembic (SKIP_DB_MIGRATE=1) ==="
fi

echo "=== Starting application ==="
if [ "$(id -u)" = "0" ] && [ "${RUN_AS_ROOT:-0}" != "1" ]; then
  exec gosu "${APP_USER}" "$@"
fi
exec "$@"
