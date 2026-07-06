import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Capacitor config for the Света shell.
 *
 * Two modes:
 *
 * 1. Runtime-selected server (default, CAP_SERVER_URL unset): the bundled
 *    launcher (public/index.html) asks for the server URL and navigates there.
 *    IMPORTANT: a site loaded this way is a "foreign" origin — Capacitor does
 *    NOT inject its native bridge into it, so native plugins (camera, biometrics,
 *    push, share) DO NOT work on the live site. `allowNavigation: ["*"]` only
 *    permits the navigation; it does not enable plugins.
 *
 * 2. Baked server (CAP_SERVER_URL set at build time, e.g. https://ptsai.ru):
 *    the site is loaded AS the Capacitor server via `server.url`, so the bridge
 *    IS injected and native plugins work. The app is then tied to that one
 *    server (the runtime picker is bypassed). This is the standard Capacitor
 *    way to ship a remote web app with working native features.
 *
 * The apk-builder passes CAP_SERVER_URL (the deployment domain) so release
 * builds get working native features; a bare `npx cap build` stays in mode 1.
 */
const SERVER_URL = process.env.CAP_SERVER_URL?.trim() || undefined;

const config: CapacitorConfig = {
  appId: "ru.aidocs.app",
  appName: "AI-DOCS",
  // Bundled launcher / offline fallback. No remote server.url is baked in.
  webDir: "public",
  server: {
    androidScheme: "https",
    allowNavigation: ["*"],
    // When set, the live site loads as the Capacitor server → native bridge is
    // injected → camera/biometrics/push work.
    ...(SERVER_URL ? { url: SERVER_URL } : {}),
  },
  android: {
    allowMixedContent: false,
  },
  plugins: {
    SpeechRecognition: {},
  },
};

export default config;
