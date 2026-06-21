#!/usr/bin/env bash
# Build the /download payload for the self-update + landing page.
#
# Usage:
#   make-release-manifest.sh <apk_path> <versionName> <versionCode> <out_dir> [changelog]
#
# Produces in <out_dir>:
#   latest.apk        — copy of the signed APK
#   version.json      — manifest the app/landing page read
# The release keystore's SHA-256 fingerprint must already be written into
# <out_dir>/.well-known/assetlinks.json by the caller (CI) if App Links are used.
set -euo pipefail

APK="${1:?apk path required}"
VERSION_NAME="${2:?versionName required}"
VERSION_CODE="${3:?versionCode required}"
OUT_DIR="${4:?out dir required}"
CHANGELOG="${5:-}"

mkdir -p "$OUT_DIR"
# Drop previous versioned copies so the release dir keeps only the current one.
find "$OUT_DIR" -maxdepth 1 -name 'AI-DOCS_*.apk' -delete 2>/dev/null || true
# latest.apk = stable URL for in-app self-update; AI-DOCS_<version>.apk = the
# human-friendly name the browser saves when downloading from /get-app.
cp "$APK" "$OUT_DIR/latest.apk"
FILE_NAME="AI-DOCS_${VERSION_NAME}.apk"
cp "$APK" "$OUT_DIR/${FILE_NAME}"

SHA256="$(sha256sum "$OUT_DIR/latest.apk" | awk '{print $1}')"

# JSON-escape the changelog without any interpreter dependency (the builder image
# has no python): escape backslashes and quotes, collapse newlines/tabs to spaces.
CHANGELOG_ESC="$(printf '%s' "$CHANGELOG" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n\r\t' '   ')"

cat > "$OUT_DIR/version.json" <<JSON
{
  "versionName": "${VERSION_NAME}",
  "versionCode": ${VERSION_CODE},
  "url": "/download/latest.apk",
  "fileName": "${FILE_NAME}",
  "sha256": "${SHA256}",
  "minSdk": 28,
  "changelog": "${CHANGELOG_ESC}"
}
JSON

echo "Wrote $OUT_DIR/version.json (sha256=$SHA256)"
