// Biometric / PIN quick-login for the mobile app.
//
// Flow: the user logs in once with the password (SSO). Then they enrol a device
// credential — the server issues an opaque {handle, secret}. On every later
// launch the device proves itself with a fingerprint or PIN and redeems that
// secret for a fresh session cookie, so the password is never asked again (until
// the device is revoked or the credential is removed).
//
// Where the secret lives:
//   • biometric — Android Keystore (via NativeBiometric.setCredentials), only
//     released after a successful fingerprint/face prompt.
//   • PIN — AES-GCM encrypted in localStorage with a key derived from the PIN
//     (PBKDF2). A wrong PIN simply fails to decrypt; the secret is never exposed.

import {
  biometricAvailable,
  biometricDeleteCredentials,
  biometricGetCredentials,
  biometricSetCredentials,
  biometricVerify,
  getAppVersion,
} from "@/lib/native-bridge";

export type QuickLoginMethod = "biometric" | "pin";

const METHOD_KEY = "quick_login_method";
const HANDLE_KEY = "quick_login_handle"; // biometric: handle lives here (secret in keystore)
const PIN_BLOB_KEY = "quick_login_pin"; // pin: { salt, iv, ct } (encrypted {handle,secret})
const PIN_LEN_KEY = "quick_login_pin_len"; // PIN digit count (for the pad's dots only)
const PBKDF2_ITER = 150_000;

export const PIN_MIN_LEN = 4;
export const PIN_MAX_LEN = 8;

function server(): string {
  return typeof window !== "undefined" ? window.location.origin : "app";
}

// ── State ─────────────────────────────────────────────────────────────────────

export function quickLoginMethod(): QuickLoginMethod | null {
  if (typeof window === "undefined") return null;
  const m = localStorage.getItem(METHOD_KEY);
  return m === "biometric" || m === "pin" ? m : null;
}

export function hasQuickLogin(): boolean {
  return quickLoginMethod() !== null;
}

// Marks that the app was unlocked in this WebView session (sessionStorage is
// empty on a cold start but survives same-origin navigations). Used so the lock
// gate doesn't re-lock immediately after the login screen already unlocked.
const UNLOCKED_FLAG = "quick_login_unlocked";

export function markUnlocked(): void {
  try {
    sessionStorage.setItem(UNLOCKED_FLAG, "1");
  } catch {
    /* ignore */
  }
}

export function wasUnlockedThisSession(): boolean {
  try {
    return sessionStorage.getItem(UNLOCKED_FLAG) === "1";
  } catch {
    return false;
  }
}

/** Configured PIN length (for the pad's dot count), or null. */
export function pinLengthHint(): number | null {
  if (typeof window === "undefined") return null;
  const v = localStorage.getItem(PIN_LEN_KEY);
  return v ? Number(v) : null;
}

export function cryptoSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.crypto?.subtle &&
    typeof TextEncoder !== "undefined"
  );
}

// ── Enrolment ─────────────────────────────────────────────────────────────────

interface EnrollResult {
  handle: string;
  secret: string;
}

async function enroll(method: QuickLoginMethod): Promise<EnrollResult> {
  const ver = await getAppVersion().catch(() => null);
  const res = await fetch("/api/auth/device-unlock/enroll", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      method,
      platform: "android",
      app_version: ver?.version ?? null,
    }),
  });
  if (!res.ok) throw new Error("Не удалось привязать устройство");
  return (await res.json()) as EnrollResult;
}

/** Revoke this device's previously enrolled credential (server-side), if any,
 * so switching methods or re-enrolling doesn't leave an orphaned valid secret. */
async function revokePreviousLocal(): Promise<void> {
  const handle = localStorage.getItem(HANDLE_KEY);
  if (!handle) return;
  try {
    await fetch("/api/auth/device-unlock/revoke", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ handle }),
    });
  } catch {
    /* best effort */
  }
}

/** Enrol biometric quick-login. Requires an authenticated session. */
export async function enableBiometric(): Promise<void> {
  if (!(await biometricAvailable()))
    throw new Error("Биометрия недоступна на этом устройстве");
  const ok = await biometricVerify("Включить вход по отпечатку");
  if (!ok) throw new Error("Подтверждение отменено");
  await revokePreviousLocal();
  const { handle, secret } = await enroll("biometric");
  const stored = await biometricSetCredentials(server(), handle, secret);
  if (!stored)
    throw new Error("Не удалось сохранить ключ в хранилище устройства");
  localStorage.setItem(HANDLE_KEY, handle);
  localStorage.setItem(METHOD_KEY, "biometric");
  localStorage.removeItem(PIN_BLOB_KEY);
  localStorage.removeItem(PIN_LEN_KEY);
}

/** Enrol PIN quick-login. Requires an authenticated session. */
export async function enablePin(pin: string): Promise<void> {
  if (!cryptoSupported()) throw new Error("Шифрование недоступно");
  await revokePreviousLocal();
  const { handle, secret } = await enroll("pin");
  const blob = await encryptWithPin(pin, JSON.stringify({ handle, secret }));
  // The handle isn't secret (useless without the secret) — keep it in the clear
  // so this credential can be revoked later even before a PIN is entered.
  localStorage.setItem(HANDLE_KEY, handle);
  localStorage.setItem(PIN_BLOB_KEY, JSON.stringify(blob));
  localStorage.setItem(PIN_LEN_KEY, String(pin.length));
  localStorage.setItem(METHOD_KEY, "pin");
  await biometricDeleteCredentials(server());
}

/** Remove quick-login on this device: revoke its credential, wipe the secret. */
export async function disableQuickLogin(): Promise<void> {
  const handle = localStorage.getItem(HANDLE_KEY);
  try {
    await fetch("/api/auth/device-unlock/revoke", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ handle }), // this device's credential only
    });
  } catch {
    /* best effort — still clear locally */
  }
  clearLocal();
  await biometricDeleteCredentials(server());
}

function clearLocal(): void {
  localStorage.removeItem(METHOD_KEY);
  localStorage.removeItem(HANDLE_KEY);
  localStorage.removeItem(PIN_BLOB_KEY);
  localStorage.removeItem(PIN_LEN_KEY);
}

// ── Unlock (redeem) ───────────────────────────────────────────────────────────

async function redeem(handle: string, secret: string): Promise<void> {
  const res = await fetch("/api/auth/device-unlock/redeem", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handle, secret }),
  });
  if (!res.ok) {
    // The credential is gone/revoked on the server — clear it so the UI falls
    // back to the password instead of offering a dead unlock button.
    if (res.status === 401) clearLocal();
    throw new Error("Быстрый вход недоступен — войдите паролем");
  }
  markUnlocked();
}

/** Unlock via biometrics: prompt, fetch the keystore secret, redeem. */
export async function unlockBiometric(): Promise<void> {
  const handle = localStorage.getItem(HANDLE_KEY);
  const ok = await biometricVerify("Вход в AI-DOCS");
  if (!ok) throw new Error("Отпечаток не распознан");
  const creds = await biometricGetCredentials(server());
  if (!creds) throw new Error("Ключ устройства не найден — войдите паролем");
  await redeem(creds.username || handle || "", creds.password);
}

/** Unlock via PIN: decrypt the stored blob, redeem. Wrong PIN → throws. */
export async function unlockPin(pin: string): Promise<void> {
  const raw = localStorage.getItem(PIN_BLOB_KEY);
  if (!raw) throw new Error("PIN не настроен");
  let handle: string, secret: string;
  try {
    const dec = await decryptWithPin(pin, JSON.parse(raw));
    ({ handle, secret } = JSON.parse(dec));
  } catch {
    throw new Error("Неверный PIN-код");
  }
  await redeem(handle, secret);
}

// ── PIN-based AES-GCM crypto ──────────────────────────────────────────────────

interface PinBlob {
  salt: string;
  iv: string;
  ct: string;
}

async function pinKey(
  pin: string,
  salt: Uint8Array<ArrayBuffer>,
): Promise<CryptoKey> {
  const base = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(pin),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITER, hash: "SHA-256" },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

async function encryptWithPin(pin: string, plain: string): Promise<PinBlob> {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await pinKey(pin, salt);
  const ct = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(plain),
  );
  return { salt: toB64(salt), iv: toB64(iv), ct: toB64(new Uint8Array(ct)) };
}

async function decryptWithPin(pin: string, blob: PinBlob): Promise<string> {
  const key = await pinKey(pin, fromB64(blob.salt));
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: fromB64(blob.iv) },
    key,
    fromB64(blob.ct),
  );
  return new TextDecoder().decode(pt);
}

function toB64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function fromB64(b64: string): Uint8Array<ArrayBuffer> {
  const s = atob(b64);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}
