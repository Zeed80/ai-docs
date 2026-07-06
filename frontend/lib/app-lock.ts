// App-lock configuration and PIN handling.
//
// The lock is a *local* gate over the existing WebView session (see
// components/mobile/BiometricGate) — it is not server authentication. Three
// modes are offered to the user: biometric (fingerprint/face), a numeric PIN,
// or off. The PIN never leaves the device: only a PBKDF2-SHA256 hash (with a
// random per-device salt) is stored in localStorage, so a casual inspection of
// storage does not reveal it. This keeps the whole feature in the web bundle —
// no APK rebuild is needed (NativeBiometric is already shipped).

export type LockMode = "off" | "biometric" | "pin";

const MODE_KEY = "app_lock_mode";
const PIN_KEY = "app_lock_pin"; // JSON: { salt, hash, iter, len }
const LEGACY_KEY = "app_lock_enabled"; // old boolean toggle

const PIN_ITERATIONS = 150_000;
export const PIN_MIN_LEN = 4;
export const PIN_MAX_LEN = 8;

interface StoredPin {
  salt: string; // base64
  hash: string; // base64
  iter: number;
  len: number;
}

// ── Mode ────────────────────────────────────────────────────────────────────

export function getLockMode(): LockMode {
  if (typeof window === "undefined") return "off";
  const raw = localStorage.getItem(MODE_KEY);
  if (raw === "off" || raw === "biometric" || raw === "pin") return raw;
  // Migrate the legacy boolean toggle. Its historical default was "on", which
  // meant biometric, so preserve that behaviour for existing installs.
  const legacy = localStorage.getItem(LEGACY_KEY);
  if (legacy === null) return "biometric";
  return legacy === "true" ? "biometric" : "off";
}

export function setLockMode(mode: LockMode): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(MODE_KEY, mode);
  // Keep the legacy key loosely in sync for anything still reading it.
  localStorage.setItem(LEGACY_KEY, String(mode !== "off"));
}

// ── PIN storage ───────────────────────────────────────────────────────────────

/** Web Crypto (subtle) is only present in secure contexts (https/native). */
export function pinSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.crypto?.subtle &&
    typeof TextEncoder !== "undefined"
  );
}

export function hasPin(): boolean {
  if (typeof window === "undefined") return false;
  return !!localStorage.getItem(PIN_KEY);
}

/** Number of digits in the configured PIN (for the pad's dot count), or null. */
export function pinLength(): number | null {
  const raw = readPin();
  return raw ? raw.len : null;
}

export function clearPin(): void {
  if (typeof window !== "undefined") localStorage.removeItem(PIN_KEY);
}

export async function setPin(pin: string): Promise<void> {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const hash = await derive(pin, salt, PIN_ITERATIONS);
  const stored: StoredPin = {
    salt: toB64(salt),
    hash,
    iter: PIN_ITERATIONS,
    len: pin.length,
  };
  localStorage.setItem(PIN_KEY, JSON.stringify(stored));
}

export async function verifyPin(pin: string): Promise<boolean> {
  const stored = readPin();
  if (!stored) return false;
  const test = await derive(pin, fromB64(stored.salt), stored.iter);
  return constantTimeEqual(test, stored.hash);
}

// ── Helpers ────────────────────────────────────────────────────────────────

function readPin(): StoredPin | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(PIN_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as StoredPin;
  } catch {
    return null;
  }
}

async function derive(
  pin: string,
  salt: Uint8Array<ArrayBuffer>,
  iterations: number,
): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(pin),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
    key,
    256,
  );
  return toB64(new Uint8Array(bits));
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
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
