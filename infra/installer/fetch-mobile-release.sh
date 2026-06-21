#!/usr/bin/env bash
#
# Pull the latest signed mobile APK + version.json from the GitHub Release and
# place them into the `releases` volume that the backend serves at /download.
#
# Best-effort: prints a warning and exits 0 on any failure so it never breaks a
# deploy. Requires curl. Honors:
#   MOBILE_RELEASE_REPO   (default: Zeed80/ai-docs)
#   AIW_COMPOSE / AIW_COMPOSE_ARGS  (compose command + args, exported by update.sh)
#
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
. "$SELF_DIR/lib.sh" 2>/dev/null || true

REPO="${MOBILE_RELEASE_REPO:-Zeed80/ai-docs}"
COMPOSE="${AIW_COMPOSE:-$(command -v docker >/dev/null && echo 'docker compose')}"
COMPOSE_ARGS="${AIW_COMPOSE_ARGS:-}"

warn_exit() { echo "  [mobile-release] $*" >&2; exit 0; }

command -v curl >/dev/null || warn_exit "curl не найден — пропускаю."

API="https://api.github.com/repos/${REPO}/releases/latest"
echo "  [mobile-release] Запрос последнего релиза $REPO…"
JSON="$(curl -fsSL "$API" 2>/dev/null)" || warn_exit "не удалось получить релиз."

apk_url="$(printf '%s' "$JSON" | grep -o '"browser_download_url": *"[^"]*latest.apk"' | head -1 | sed 's/.*"\(http[^"]*\)"/\1/')"
ver_url="$(printf '%s' "$JSON" | grep -o '"browser_download_url": *"[^"]*version.json"' | head -1 | sed 's/.*"\(http[^"]*\)"/\1/')"
[ -n "$apk_url" ] && [ -n "$ver_url" ] || warn_exit "в релизе нет latest.apk/version.json."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$apk_url" -o "$TMP/latest.apk" || warn_exit "скачивание APK не удалось."
curl -fsSL "$ver_url" -o "$TMP/version.json" || warn_exit "скачивание version.json не удалось."

# Copy into the backend container's /releases (the mounted volume).
# shellcheck disable=SC2086
$COMPOSE $COMPOSE_ARGS cp "$TMP/latest.apk" backend:/releases/latest.apk \
  || warn_exit "не удалось скопировать APK в контейнер backend."
# shellcheck disable=SC2086
$COMPOSE $COMPOSE_ARGS cp "$TMP/version.json" backend:/releases/version.json \
  || warn_exit "не удалось скопировать version.json."

echo "  [mobile-release] APK и version.json обновлены в /releases."
