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
cp "$APK" "$OUT_DIR/latest.apk"

SHA256="$(sha256sum "$OUT_DIR/latest.apk" | awk '{print $1}')"

cat > "$OUT_DIR/version.json" <<JSON
{
  "versionName": "${VERSION_NAME}",
  "versionCode": ${VERSION_CODE},
  "url": "/download/latest.apk",
  "sha256": "${SHA256}",
  "minSdk": 28,
  "changelog": $(printf '%s' "$CHANGELOG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
}
JSON

echo "Wrote $OUT_DIR/version.json (sha256=$SHA256)"
