import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Capacitor config for the Света shell.
 *
 * The app is NOT tied to any server at build time. On first launch the bundled
 * launcher (public/index.html) asks the user for the server URL (typed or scanned
 * from a QR code); it's saved by the ServerConfig plugin and the WebView then
 * loads that live site. Every frontend deploy is picked up without reinstalling.
 *
 * `allowNavigation: ["*"]` lets the WebView navigate to whatever server the user
 * chose (instead of bouncing it to an external browser); the user controls which
 * host that is.
 */
const config: CapacitorConfig = {
  appId: "ru.aiworkspace.sveta",
  appName: "Света",
  // Bundled launcher / offline fallback. No remote server.url is baked in.
  webDir: "public",
  server: {
    androidScheme: "https",
    allowNavigation: ["*"],
  },
  android: {
    allowMixedContent: false,
  },
  plugins: {
    SpeechRecognition: {},
  },
};

export default config;
