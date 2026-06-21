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

// ── QR scanning (login / server config) ───────────────────────────────────────

/** Scan a single QR/barcode with the native scanner. Returns its raw value. */
export async function scanQr(): Promise<string | null> {
  const scanner = plugin("BarcodeScanner");
  if (!scanner?.scan) return null;
  try {
    await scanner.requestPermissions?.();
    const res = await scanner.scan();
    const code = res?.barcodes?.[0]?.rawValue;
    return (code as string) ?? null;
  } catch (e) {
    console.warn("scanQr failed", e);
    return null;
  }
}

// ── Camera / document scan ────────────────────────────────────────────────────

/**
 * Capture document pages. Prefers a native multi-page document scanner
 * (auto-crop / perspective / b&w); falls back to the Camera plugin, then to a
 * plain web file input. Returns the captured files (caller uploads them).
 */
export async function scanDocument(): Promise<File[]> {
  // 1) Native document scanner (multi-page → images or a PDF).
  const scanner = plugin("DocumentScanner");
  if (scanner?.scanDocument) {
    try {
      const result = await scanner.scanDocument({
        responseType: "imageFilePath",
      });
      const uris: string[] = result?.scannedImages ?? result?.images ?? [];
      if (uris.length) {
        return Promise.all(
          uris.map((u, i) => uriToFile(u, `scan-${Date.now()}-${i + 1}.jpg`)),
        );
      }
    } catch (e) {
      console.warn("DocumentScanner failed, falling back to camera", e);
    }
  }

  // 2) Camera plugin (single photo).
  const camera = plugin("Camera");
  if (camera?.getPhoto) {
    try {
      const photo = await camera.getPhoto({
        quality: 85,
        allowEditing: false,
        resultType: "base64",
        source: "CAMERA",
        saveToGallery: false,
      });
      if (photo?.base64String) {
        return [b64ToFile(photo.base64String, `photo-${Date.now()}.jpg`)];
      }
      if (photo?.webPath) {
        return [await uriToFile(photo.webPath, `photo-${Date.now()}.jpg`)];
      }
    } catch (e) {
      console.warn("Camera plugin failed, falling back to web input", e);
    }
  }

  // 3) Web fallback — system file picker with camera capture hint.
  return pickFilesWeb({ accept: "image/*", capture: "environment" });
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
 * Register this device for push. The native SvetaPush plugin owns the ntfy topic
 * and its foreground subscription; we just persist the topic on the backend so
 * notifications can be addressed to this user. No-op on the web.
 */
export async function registerForPush(appVersion?: string): Promise<void> {
  const push = plugin("SvetaPush");
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
    await bio.verifyIdentity({ reason, title: "Света", subtitle: reason });
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

export async function checkForUpdate(): Promise<UpdateInfo> {
  const updater = plugin("AppUpdate");
  if (!updater?.checkForUpdate) return { available: false };
  try {
    return await updater.checkForUpdate({ url: VERSION_URL });
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
