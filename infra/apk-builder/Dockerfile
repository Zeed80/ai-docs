# Self-contained Android APK builder for the Света shell.
# Bundles JDK 21 + Node 22 + Android SDK (platform/build-tools 36) and the mobile
# source, so the backend can (re)build a signed APK on demand via the Docker socket
# and publish it to the `releases` volume (served at /download). No host toolchain.
#
# Build context = repo root (so it can COPY mobile/). One-time heavy image build;
# rebuilt by update.sh on `git pull` to pick up shell source changes.
FROM eclipse-temurin:21-jdk

ENV ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_HOME=/opt/android-sdk \
    NODE_VERSION=22.12.0 \
    DEBIAN_FRONTEND=noninteractive \
    GRADLE_USER_HOME=/build/.gradle

# Base tools + Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl unzip xz-utils ca-certificates git \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && mkdir -p /opt/node && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && ln -s /opt/node/bin/node /usr/local/bin/node \
    && ln -s /opt/node/bin/npm /usr/local/bin/npm \
    && ln -s /opt/node/bin/npx /usr/local/bin/npx \
    && rm -rf /var/lib/apt/lists/*

# Android SDK: cmdline-tools + platform/build-tools 36 (+34 for plugin compat)
RUN mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools" \
    && curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -o /tmp/cli.zip \
    && unzip -q /tmp/cli.zip -d "$ANDROID_SDK_ROOT/cmdline-tools" \
    && mv "$ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools" "$ANDROID_SDK_ROOT/cmdline-tools/latest" \
    && rm /tmp/cli.zip
ENV PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH"
RUN yes 2>/dev/null | sdkmanager --licenses >/dev/null 2>&1 || true \
    && sdkmanager "platform-tools" "platforms;android-36" "build-tools;36.0.0" >/dev/null

# Shell source + warmed deps / generated android project (cached in the image)
COPY mobile/ /build/mobile/
WORKDIR /build/mobile
# Start clean even if host-generated dirs slipped into the context.
RUN rm -rf android node_modules \
    && npm ci \
    && npx cap add android \
    && scripts/prepare-android.sh \
    && npx cap sync android

COPY infra/apk-builder/build.sh /usr/local/bin/build-apk
RUN chmod +x /usr/local/bin/build-apk

ENTRYPOINT ["build-apk"]
