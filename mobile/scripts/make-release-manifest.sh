#!/usr/bin/env bash
# Build the /download payload for the self-update + landing page.
#
# Usage:
#   make-release-manifest.sh <apk_path> <versionName> <versionCode> <out_dir> [changelog]
#
# Optional signing env:
#   MOBILE_MANIFEST_KEYSTORE=/path/release.jks
#   MOBILE_MANIFEST_KEYSTORE_PASSWORD=...
#   MOBILE_MANIFEST_KEY_ALIAS=...
#   MOBILE_MANIFEST_KEY_PASSWORD=...   # defaults to store password
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

# JSON-escape strings without any interpreter dependency (the builder image has no
# python): escape backslashes and quotes, collapse newlines/tabs to spaces.
json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n\r\t' '   '
}

VERSION_NAME_ESC="$(json_escape "$VERSION_NAME")"
FILE_NAME_ESC="$(json_escape "$FILE_NAME")"
CHANGELOG_ESC="$(json_escape "$CHANGELOG")"

SIGNED_PAYLOAD_JSON="$(printf '{"versionName":"%s","versionCode":%s,"url":"/download/latest.apk","fileName":"%s","sha256":"%s","minSdk":28,"changelog":"%s"}' \
  "$VERSION_NAME_ESC" "$VERSION_CODE" "$FILE_NAME_ESC" "$SHA256" "$CHANGELOG_ESC")"
SIGNED_PAYLOAD_B64="$(printf '%s' "$SIGNED_PAYLOAD_JSON" | base64 | tr -d '\n')"
SIGNATURE=""
SIGNATURE_ALG=""

if [ -n "${MOBILE_MANIFEST_KEYSTORE:-}" ]; then
  : "${MOBILE_MANIFEST_KEYSTORE_PASSWORD:?MOBILE_MANIFEST_KEYSTORE_PASSWORD required}"
  : "${MOBILE_MANIFEST_KEY_ALIAS:?MOBILE_MANIFEST_KEY_ALIAS required}"
  KEY_PASS="${MOBILE_MANIFEST_KEY_PASSWORD:-$MOBILE_MANIFEST_KEYSTORE_PASSWORD}"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  printf '%s' "$SIGNED_PAYLOAD_JSON" > "$TMP_DIR/payload.json"
  cat > "$TMP_DIR/SignPayload.java" <<'JAVA'
import java.io.FileInputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.Signature;
import java.util.Base64;

public class SignPayload {
  public static void main(String[] args) throws Exception {
    String keystorePath = args[0];
    char[] storePass = args[1].toCharArray();
    String alias = args[2];
    char[] keyPass = args[3].toCharArray();
    byte[] payload = Files.readAllBytes(Path.of(args[4]));

    KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
    try (FileInputStream in = new FileInputStream(keystorePath)) {
      ks.load(in, storePass);
    }
    PrivateKey key = (PrivateKey) ks.getKey(alias, keyPass);
    Signature sig = Signature.getInstance("SHA256withRSA");
    sig.initSign(key);
    sig.update(payload);
    System.out.print(Base64.getEncoder().encodeToString(sig.sign()));
  }
}
JAVA
  javac "$TMP_DIR/SignPayload.java"
  SIGNATURE="$(java -cp "$TMP_DIR" SignPayload "$MOBILE_MANIFEST_KEYSTORE" "$MOBILE_MANIFEST_KEYSTORE_PASSWORD" "$MOBILE_MANIFEST_KEY_ALIAS" "$KEY_PASS" "$TMP_DIR/payload.json")"
  SIGNATURE_ALG="SHA256withRSA"
fi

cat > "$OUT_DIR/version.json" <<JSON
{
  "versionName": "${VERSION_NAME_ESC}",
  "versionCode": ${VERSION_CODE},
  "url": "/download/latest.apk",
  "fileName": "${FILE_NAME_ESC}",
  "sha256": "${SHA256}",
  "minSdk": 28,
  "changelog": "${CHANGELOG_ESC}",
  "signedPayload": "${SIGNED_PAYLOAD_B64}",
  "signatureAlg": "${SIGNATURE_ALG}",
  "signature": "${SIGNATURE}"
}
JSON

echo "Wrote $OUT_DIR/version.json (sha256=$SHA256)"
