/**
 * Native bridge — connects the web app to Capacitor plugins when running inside
 * the Android shell. All plugin access is via the runtime-injected
 * `window.Capacitor` so the frontend keeps NO `@capacitor/*` build dependency:
 * the same bundle runs in a normal browser (where every call degrades to a no-op
 * or web fallback) and inside the APK (where plugins are present).
 *
 * Because all logic lives here in the frontend, it ships with every site deploy —
 * the APK only needs reinstalling when the *set* of plugins changes.
 */

import { mutFetch } from "@/lib/auth";

/* eslint-disable @typescript-eslint/no-explicit-any */

type Cap = {
  isNativePlatform?: () => boolean;
  getPlatform?: () => string;
  convertFileSrc?: (url: string) => string;
  Plugins?: Record<string, any>;
};

function cap(): Cap | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as any).Capacitor as Cap | undefined;
}

function plugin<T = any>(name: string): T | undefined {
  return cap()?.Plugins?.[name] as T | undefined;
}

export function isNative(): boolean {
  const c = cap();
  return !!c?.isNativePlatform?.() || !!c?.Plugins;
}

export function platform(): string {
  return cap()?.getPlatform?.() ?? "web";
}

/** Turn a native file:// URI or data URI into a File for upload. */
async function uriToFile(
  uri: string,
  name: string,
  mime = "image/jpeg",
): Promise<File> {
  const src = uri.startsWith("data:")
    ? uri
    : (cap()?.convertFileSrc?.(uri) ?? uri);
  const res = await fetch(src);
  const blob = await res.blob();
  return new File([blob], name, { type: blob.type || mime });
}

function b64ToFile(base64: string, name: string, mime = "image/jpeg"): File {
  const clean = base64.includes(",") ? base64.split(",")[1] : base64;
  const bytes = atob(clean);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new File([arr], name, { type: mime });
}

// ── Server configuration (no hardcoded host) ──────────────────────────────────

/** The server URL the user configured at first launch (native only). */
export async function getServerConfig(): Promise<string | null> {
  const sc = plugin("ServerConfig");
  if (!sc?.get) return null;
  try {
    const r = await sc.get();
    return (r?.url as string) ?? null;
  } catch {
    return null;
  }
}

/** Persist a new server URL and reload the app onto it (native only). */
export async function setServerConfig(url: string): Promise<void> {
  const sc = plugin("ServerConfig");
  if (!sc?.set) return;
  const r = await sc.set({ url });
  const target = (r?.url as string) ?? url;
  if (typeof window !== "undefined") window.location.replace(target);
}

/** Forget the configured server and return to the launcher/setup screen.
 *  The native plugin recreates the activity, which reloads the bundled launcher. */
export async function clearServerConfig(): Promise<void> {
  const sc = plugin("ServerConfig");
  if (!sc?.clear) return;
  await sc.clear();
}

/** Consume a deep-link path stashed by a push tap during cold start (one-shot). */
export async function consumePendingPath(): Promise<string | null> {
  const sc = plugin("ServerConfig");
  if (!sc?.consumePendingPath) return null;
  try {
    const r = await sc.consumePendingPath();
    return (r?.path as string) ?? null;
  } catch {
    return null;
  }
}

// ── QR scanning (login / server config) ───────────────────────────────────────

/**
 * Scan a single QR with the native scanner. Returns its raw value (or null).
 *
 * Uses the plugin's `startScan()` (CameraX + the BUNDLED ML Kit model) rather than
 * `scan()` — the latter relies on the Google Code Scanner module fetched via Play
 * Services (ModuleInstall), which fails on GMS-less devices (HONOR/Huawei: error
 * "17: ModuleInstall.API is not available"). The bundled model works everywhere.
 *
 * The camera renders behind the WebView, so we make the page transparent and show
 * a minimal viewfinder + cancel overlay while scanning.
 */
export async function scanQr(): Promise<string | null> {
  const scanner = plugin("BarcodeScanner");
  if (!scanner?.startScan) {
    // Fallback for older plugin builds (GMS devices only).
    if (scanner?.scan) {
      try {
        await scanner.requestPermissions?.();
        const res = await scanner.scan();
        return (res?.barcodes?.[0]?.rawValue as string) ?? null;
      } catch {
        return null;
      }
    }
    return null;
  }

  try {
    const perm = await scanner.requestPermissions?.();
    if (
      perm &&
      perm.camera &&
      perm.camera !== "granted" &&
      perm.camera !== "limited"
    ) {
      return null;
    }
  } catch {
    /* continue — startScan will surface a hard failure */
  }

  return new Promise<string | null>((resolve) => {
    let settled = false;
    const overlay = buildScanOverlay(() => finish(null));
    document.documentElement.classList.add("qr-scanning");
    document.body.appendChild(overlay);

    const finish = async (value: string | null) => {
      if (settled) return;
      settled = true;
      try {
        await scanner.removeAllListeners?.();
      } catch {
        /* ignore */
      }
      try {
        await scanner.stopScan?.();
      } catch {
        /* ignore */
      }
      document.documentElement.classList.remove("qr-scanning");
      overlay.remove();
      resolve(value);
    };

    scanner
      .addListener?.("barcodeScanned", (event: any) => {
        const code = event?.barcode?.rawValue ?? event?.barcodes?.[0]?.rawValue;
        if (code) void finish(code as string);
      })
      ?.catch?.(() => {});

    Promise.resolve(scanner.startScan?.()).catch((e: unknown) => {
      console.warn("startScan failed", e);
      void finish(null);
    });

    // Safety timeout so the scanner never hangs forever.
    setTimeout(() => void finish(null), 60000);
  });
}

/** Minimal transparent overlay (viewfinder frame + cancel) shown during scanning. */
function buildScanOverlay(onCancel: () => void): HTMLDivElement {
  const overlay = document.createElement("div");
  overlay.className = "qr-scan-overlay";
  overlay.innerHTML = `
    <div class="qr-scan-frame"></div>
    <button type="button" class="qr-scan-cancel">Отмена</button>
  `;
  overlay.querySelector(".qr-scan-cancel")?.addEventListener("click", onCancel);
  return overlay;
}

// ── Camera / document scan ────────────────────────────────────────────────────

/**
 * Capture a document photo with the device camera. Uses the @capacitor/camera
 * plugin (system camera / CameraX) — GMS-free, so it works on devices WITHOUT
 * Google services (HONOR/Huawei). The previous ML Kit DocumentScanner required
 * Google Play Services and hung on GMS-less devices, so it's no longer used.
 * Returns the captured file(s); empty array if the user cancels.
 */
export async function scanDocument(): Promise<File[]> {
  const camera = plugin("Camera");
  if (camera?.getPhoto) {
    try {
      const photo = await camera.getPhoto({
        quality: 85,
        allowEditing: false,
        resultType: "base64",
        source: "CAMERA", // open the camera directly, never the gallery
        saveToGallery: false,
      });
      if (photo?.base64String) {
        return [b64ToFile(photo.base64String, `photo-${Date.now()}.jpg`)];
      }
      if (photo?.dataUrl) {
        return [b64ToFile(photo.dataUrl, `photo-${Date.now()}.jpg`)];
      }
      if (photo?.webPath || photo?.path) {
        return [
          await uriToFile(
            (photo.webPath || photo.path) as string,
            `photo-${Date.now()}.jpg`,
          ),
        ];
      }
      return []; // nothing returned
    } catch (e) {
      // User cancelled or camera error — do NOT fall back to a gallery/file
      // picker (that's what caused the "opens gallery then hangs" report).
      console.warn("camera getPhoto cancelled/failed", e);
      return [];
    }
  }

  // Non-native (browser): system file picker with camera-capture hint.
  return pickFilesWeb({ accept: "image/*", capture: "environment" });
}

/**
 * Pick an image from the camera OR the gallery (image studio). Unlike
 * scanDocument (camera-only), this lets the user choose an existing photo —
 * the @capacitor/camera plugin already supports both via the `source` param,
 * so no extra plugin is needed. Falls back to a web file picker in the browser.
 */
export async function pickImage(
  source: "CAMERA" | "PHOTOS" = "PHOTOS",
): Promise<File[]> {
  const camera = plugin("Camera");
  if (camera?.getPhoto) {
    try {
      const photo = await camera.getPhoto({
        quality: 90,
        allowEditing: false,
        resultType: "base64",
        source,
        saveToGallery: false,
      });
      if (photo?.base64String) {
        return [b64ToFile(photo.base64String, `image-${Date.now()}.jpg`)];
      }
      if (photo?.dataUrl) {
        return [b64ToFile(photo.dataUrl, `image-${Date.now()}.jpg`)];
      }
      if (photo?.webPath || photo?.path) {
        return [
          await uriToFile(
            (photo.webPath || photo.path) as string,
            `image-${Date.now()}.jpg`,
          ),
        ];
      }
      return [];
    } catch (e) {
      console.warn("camera pickImage cancelled/failed", e);
      return [];
    }
  }
  return pickFilesWeb({
    accept: "image/*",
    capture: source === "CAMERA" ? "environment" : undefined,
    multiple: false,
  });
}

/** Open a web file picker (used as a fallback and on desktop). */
export function pickFilesWeb(opts?: {
  accept?: string;
  capture?: string;
  multiple?: boolean;
}): Promise<File[]> {
  return new Promise((resolve) => {
    if (typeof document === "undefined") return resolve([]);
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = opts?.multiple ?? true;
    if (opts?.accept) input.accept = opts.accept;
    if (opts?.capture) input.setAttribute("capture", opts.capture);
    input.onchange = () => resolve(input.files ? Array.from(input.files) : []);
    input.click();
  });
}

// ── Share / "Open with" intake ────────────────────────────────────────────────

export interface SharedPayload {
  files: File[];
  text?: string;
  title?: string;
}

/**
 * Pull a file/text shared into the app from another app (Email/Telegram/MAX…).
 * Returns null when nothing was shared. Backed by the SendIntent plugin.
 */
export async function consumeSharedIntent(): Promise<SharedPayload | null> {
  const sendIntent = plugin("SendIntent");
  if (!sendIntent?.checkSendIntentReceived) return null;
  try {
    const result = await sendIntent.checkSendIntentReceived();
    if (!result) return null;
    const files: File[] = [];
    const items: any[] =
      result.files ?? (result.url ? [{ url: result.url }] : []);
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const url = it.url ?? it.path;
      if (!url) continue;
      const name = it.title ?? it.name ?? `shared-${Date.now()}-${i + 1}`;
      try {
        files.push(
          await uriToFile(url, name, it.type ?? "application/octet-stream"),
        );
      } catch (e) {
        console.warn("Failed to read shared file", url, e);
      }
    }
    return { files, text: result.text, title: result.title };
  } catch (e) {
    console.warn("consumeSharedIntent failed", e);
    return null;
  }
}

/** Share a downloaded export (Excel/1С/PDF) out to other apps. */
export async function shareOut(opts: {
  title?: string;
  url?: string;
  text?: string;
}): Promise<void> {
  const share = plugin("Share");
  if (share?.share) {
    try {
      await share.share(opts);
      return;
    } catch (e) {
      console.warn("native share failed", e);
    }
  }
  if (
    typeof navigator !== "undefined" &&
    (navigator as any).share &&
    opts.url
  ) {
    try {
      await (navigator as any).share({
        title: opts.title,
        text: opts.text,
        url: opts.url,
      });
    } catch {
      /* user cancelled */
    }
  }
}

// ── Push registration (self-hosted ntfy) ──────────────────────────────────────

/**
 * Register this device for push. The native AidocsPush plugin owns the ntfy topic
 * and its foreground subscription; we just persist the topic on the backend so
 * notifications can be addressed to this user. No-op on the web.
 */
export async function registerForPush(appVersion?: string): Promise<void> {
  const push = plugin("AidocsPush");
  if (!push?.register) return;
  try {
    const reg = await push.register(); // { topic, endpoint? }
    if (!reg?.topic) return;
    const res = await mutFetch("/api/devices/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ntfy_topic: reg.topic,
        ntfy_endpoint: reg.endpoint ?? null,
        platform: platform(),
        app_version: appVersion ?? null,
      }),
    });
    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      // Hand the resolved external ntfy URL back so native can (re)subscribe.
      if (data?.ntfy_url && push.configure) {
        await push.configure({ url: data.ntfy_url, topic: reg.topic });
      }
    }
  } catch (e) {
    console.warn("registerForPush failed", e);
  }
}

// ── Biometric app-lock ─────────────────────────────────────────────────────────

export async function biometricAvailable(): Promise<boolean> {
  const bio = plugin("NativeBiometric");
  if (!bio?.isAvailable) return false;
  try {
    const r = await bio.isAvailable();
    return !!r?.isAvailable;
  } catch {
    return false;
  }
}

export async function biometricVerify(
  reason = "Подтвердите личность",
): Promise<boolean> {
  const bio = plugin("NativeBiometric");
  if (!bio?.verifyIdentity) return true; // nothing to enforce → allow
  try {
    await bio.verifyIdentity({ reason, title: "AI-DOCS", subtitle: reason });
    return true;
  } catch {
    return false;
  }
}

// ── Speech-to-text (voice input) ───────────────────────────────────────────────

export async function speechAvailable(): Promise<boolean> {
  const sr = plugin("SpeechRecognition");
  if (sr?.available) {
    try {
      const r = await sr.available();
      return !!(r?.available ?? r);
    } catch {
      return false;
    }
  }
  // Web Speech API fallback (works in some WebViews/browsers).
  return (
    typeof window !== "undefined" &&
    !!(
      (window as any).SpeechRecognition ||
      (window as any).webkitSpeechRecognition
    )
  );
}

/**
 * Single-shot dictation. Resolves with the recognized text (RU by default).
 * Uses the native plugin when present, otherwise the Web Speech API.
 */
export async function dictate(lang = "ru-RU"): Promise<string> {
  const sr = plugin("SpeechRecognition");
  if (sr?.start) {
    try {
      await sr.requestPermissions?.();
      const r = await sr.start({
        language: lang,
        maxResults: 1,
        partialResults: false,
        popup: false,
      });
      const matches: string[] = r?.matches ?? [];
      return matches[0] ?? "";
    } catch (e) {
      console.warn("native dictate failed", e);
      return "";
    }
  }
  // Web Speech API fallback.
  return new Promise((resolve) => {
    const Ctor =
      (window as any).SpeechRecognition ||
      (window as any).webkitSpeechRecognition;
    if (!Ctor) return resolve("");
    const rec = new Ctor();
    rec.lang = lang;
    rec.maxAlternatives = 1;
    rec.interimResults = false;
    rec.onresult = (ev: any) => resolve(ev.results?.[0]?.[0]?.transcript ?? "");
    rec.onerror = () => resolve("");
    rec.onend = () => resolve("");
    rec.start();
  });
}

// ── App self-update ────────────────────────────────────────────────────────────

export interface UpdateInfo {
  available: boolean;
  versionName?: string;
  versionCode?: number;
  changelog?: string;
}

const VERSION_URL = "/download/version.json";

/**
 * Check for a newer build. Compares the served version.json against the installed
 * versionCode (App plugin's `build`) directly in JS — robust and debuggable, and
 * independent of the native AppUpdate check. Only the INSTALL uses the native
 * plugin (downloadAndInstall). No-op off the native shell.
 */
export async function checkForUpdate(): Promise<UpdateInfo> {
  if (!isNative()) return { available: false };
  try {
    // Cache-bust + no-store so we never read a stale manifest.
    const res = await fetch(`${VERSION_URL}?_=${Date.now()}`, {
      cache: "no-store",
    });
    if (!res.ok) return { available: false };
    const v = await res.json();

    let installed = 0;
    const app = plugin("App");
    if (app?.getInfo) {
      try {
        const info = await app.getInfo();
        installed = parseInt(String(info?.build ?? "0"), 10) || 0; // Android build == versionCode
      } catch {
        /* ignore */
      }
    }

    const latest = typeof v?.versionCode === "number" ? v.versionCode : 0;
    return {
      available: latest > installed,
      versionName: v?.versionName,
      versionCode: latest,
      changelog: v?.changelog,
    };
  } catch (e) {
    console.warn("checkForUpdate failed", e);
    return { available: false };
  }
}

export async function installUpdate(): Promise<void> {
  const updater = plugin("AppUpdate");
  if (!updater?.downloadAndInstall) return;
  try {
    await updater.downloadAndInstall({ url: VERSION_URL });
  } catch (e) {
    console.warn("installUpdate failed", e);
  }
}

/** Installed app version/build (from the Capacitor App plugin). Null on web. */
export async function getAppVersion(): Promise<{
  version?: string;
  build?: string;
} | null> {
  const app = plugin("App");
  if (!app?.getInfo) return null;
  try {
    const info = await app.getInfo();
    return {
      version: info?.version,
      build: info?.build != null ? String(info.build) : undefined,
    };
  } catch {
    return null;
  }
}
