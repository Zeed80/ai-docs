#!/usr/bin/env bash
# Entry point of the apk-builder image. Builds a SIGNED release APK and publishes
# latest.apk + version.json to the mounted /releases volume (served at /download).
#
# A persistent keystore lives in the mounted /keystore volume so every rebuild is
# signed with the SAME key — required for in-app "install over the top" updates.
#
# Env: VERSION_NAME, VERSION_CODE (injected by the backend).
set -euo pipefail

VERSION_NAME="${VERSION_NAME:-0.1.0}"
VERSION_CODE="${VERSION_CODE:-1}"

KS_DIR=/keystore
KS="$KS_DIR/aidocs-release.jks"
KS_PASS_FILE="$KS_DIR/pass"
mkdir -p "$KS_DIR" /releases

# Stable signing key (generate once, persist).
if [ ! -f "$KS" ]; then
  KS_PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
  printf '%s' "$KS_PASS" > "$KS_PASS_FILE"
  keytool -genkeypair -v -keystore "$KS" -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -alias aidocs -keyalg RSA -keysize 4096 -validity 10000 \
    -dname "CN=AI-DOCS, O=AI-DOCS" >/dev/null 2>&1
  echo "[apk-builder] created persistent release keystore"
fi
KS_PASS="$(cat "$KS_PASS_FILE")"

cd /build/mobile
# Re-apply overrides (idempotent) and inject the version.
scripts/prepare-android.sh
sed -i "s/versionCode .*/versionCode ${VERSION_CODE}/" android/app/build.gradle
sed -i "s/versionName \".*\"/versionName \"${VERSION_NAME}\"/" android/app/build.gradle
# Launcher label stays "AI-DOCS" (from capacitor appName); the version lives only
# in the downloaded file name (AI-DOCS_<version>.apk), not in the app label.

echo "[apk-builder] building release ${VERSION_NAME} (${VERSION_CODE})…"
cd android
./gradlew assembleRelease --no-daemon \
  -Pandroid.injected.signing.store.file="$KS" \
  -Pandroid.injected.signing.store.password="$KS_PASS" \
  -Pandroid.injected.signing.key.alias=aidocs \
  -Pandroid.injected.signing.key.password="$KS_PASS"

APK="$(find "$PWD/app/build/outputs/apk/release" -name '*.apk' | head -1)"
[ -n "$APK" ] || { echo "[apk-builder] APK not found" >&2; exit 1; }

cd /build/mobile
MOBILE_MANIFEST_KEYSTORE="$KS" \
MOBILE_MANIFEST_KEYSTORE_PASSWORD="$KS_PASS" \
MOBILE_MANIFEST_KEY_ALIAS=aidocs \
MOBILE_MANIFEST_KEY_PASSWORD="$KS_PASS" \
  scripts/make-release-manifest.sh "$APK" "$VERSION_NAME" "$VERSION_CODE" /releases "Сборка с сервера ${VERSION_NAME}"
echo "[apk-builder] published /releases/latest.apk + version.json"
