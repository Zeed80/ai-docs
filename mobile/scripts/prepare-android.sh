#!/usr/bin/env bash
#
# Apply all post-`npx cap add android` patches that the Света shell needs, on top
# of the generated (gitignored) android/ project. Idempotent — safe to re-run.
#
# Run from mobile/ after `npx cap add android`:
#   scripts/prepare-android.sh
#
# Steps:
#   1. Remove the generated Java MainActivity (we ship a Kotlin one in overrides).
#   2. Copy android-overrides/* (manifest, FileProvider, Kotlin plugins).
#   3. Enable Kotlin (root classpath + app plugin).
#   4. minSdk 28 (Android 9+).
#   5. Force all library subprojects to compileSdk 36 (some plugins, e.g.
#      send-intent, hardcode 35, which androidx.core 1.17 rejects).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$ROOT"
[ -d android ] || { echo "android/ not found — run 'npx cap add android' first." >&2; exit 1; }

# 1. Drop generated Java MainActivity (conflicts with overrides' Kotlin one).
find android/app/src/main/java -name MainActivity.java -delete 2>/dev/null || true

# 2. Overlay our native files.
cp -r android-overrides/* android/

# 3. Kotlin: root classpath.
if ! grep -q "kotlin-gradle-plugin" android/build.gradle; then
  sed -i "s#classpath 'com.google.gms:google-services:4.4.4'#&\n        classpath 'org.jetbrains.kotlin:kotlin-gradle-plugin:2.0.21'#" android/build.gradle
fi
# 3b. Kotlin: app plugin.
if ! grep -q "kotlin-android" android/app/build.gradle; then
  sed -i "s#apply plugin: 'com.android.application'#&\napply plugin: 'kotlin-android'#" android/app/build.gradle
fi

# 4. minSdk 28 (Android 9+).
sed -i -E "s/minSdkVersion = [0-9]+/minSdkVersion = 28/" android/variables.gradle

# 5. Force compileSdk 36 on all Android subprojects.
if ! grep -q "Force all Android library subprojects" android/build.gradle; then
  cat >> android/build.gradle <<'GRADLE'

// Force all Android library subprojects (some Capacitor plugins, e.g. send-intent,
// hardcode compileSdk 35) up to 36, which androidx.core 1.17 requires.
subprojects {
    afterEvaluate { project ->
        if (project.hasProperty('android')) {
            project.android {
                compileSdkVersion 36
            }
        }
    }
}
GRADLE
fi

echo "android/ prepared (Kotlin enabled, minSdk 28, compileSdk 36, overrides applied)."
