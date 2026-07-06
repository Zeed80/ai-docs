"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  getLockMode,
  hasPin,
  pinLength,
  pinSupported,
  verifyPin,
} from "@/lib/app-lock";
import PinPad from "@/components/mobile/PinPad";
import {
  biometricAvailable,
  biometricVerify,
  isNative,
} from "@/lib/native-bridge";

const LOCK_TIMEOUT_MS = 2 * 60 * 1000; // re-lock after 2 min in background

type Method = "biometric" | "pin";

/**
 * App-lock overlay for the native shell. Locks on cold start and after the app
 * has been backgrounded past the timeout. The user picks the method in settings:
 * biometric (fingerprint/face) or a numeric PIN. Biometric falls back to the PIN
 * when a PIN is also configured or when no sensor is available. This is a purely
 * local gate over the existing WebView session — not server authentication.
 */
export function BiometricGate() {
  const [checking, setChecking] = useState(true);
  const [locked, setLocked] = useState(false);
  // Which method the overlay is currently offering.
  const [method, setMethod] = useState<Method>("biometric");
  // Whether a PIN fallback exists (shows the "enter PIN" affordance).
  const [pinAvailable, setPinAvailable] = useState(false);

  const [pin, setPin] = useState("");
  const [pinError, setPinError] = useState(false);
  const expectedLen = useRef<number | null>(null);

  // Decide whether the lock applies and via which method.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isNative()) {
        setChecking(false);
        return;
      }
      const mode = getLockMode();
      const pinReady = pinSupported() && hasPin();
      const bioReady = await biometricAvailable();
      if (cancelled) return;

      let active = false;
      let primary: Method = "biometric";
      if (mode === "pin" && pinReady) {
        active = true;
        primary = "pin";
      } else if (mode === "biometric") {
        if (bioReady) {
          active = true;
          primary = "biometric";
        } else if (pinReady) {
          // Chosen biometric, but no sensor — fall back to the PIN if set.
          active = true;
          primary = "pin";
        }
      }

      expectedLen.current = pinLength();
      setPinAvailable(pinReady);
      setMethod(primary);
      setLocked(active);
      setChecking(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const tryBiometric = useCallback(async () => {
    const ok = await biometricVerify("Разблокировать AI-DOCS");
    if (ok) {
      setLocked(false);
      setPin("");
    } else if (pinAvailable) {
      // User cancelled or failed — offer the PIN pad instead of a dead end.
      setMethod("pin");
    }
  }, [pinAvailable]);

  // Auto-prompt biometrics when the overlay shows in biometric mode.
  useEffect(() => {
    if (!checking && locked && method === "biometric") void tryBiometric();
  }, [checking, locked, method, tryBiometric]);

  // Verify the PIN as soon as it reaches the configured length.
  useEffect(() => {
    if (method !== "pin" || !expectedLen.current) return;
    if (pin.length < expectedLen.current) return;
    let cancelled = false;
    void verifyPin(pin).then((ok) => {
      if (cancelled) return;
      if (ok) {
        setLocked(false);
        setPin("");
        setPinError(false);
      } else {
        setPinError(true);
        setTimeout(() => {
          setPin("");
          setPinError(false);
        }, 600);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [pin, method]);

  // Re-lock when returning from the background after the timeout.
  useEffect(() => {
    if (checking || !isNative()) return;
    let hiddenAt = 0;
    function onVisibility() {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else if (hiddenAt && Date.now() - hiddenAt > LOCK_TIMEOUT_MS) {
        const mode = getLockMode();
        if (mode !== "off") {
          setPin("");
          setMethod(mode === "pin" ? "pin" : "biometric");
          setLocked(true);
        }
      }
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [checking]);

  if (checking || !locked) return null;

  return (
    <div className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-8 bg-slate-950 px-6 text-slate-100">
      {method === "biometric" ? (
        <>
          <div className="flex h-20 w-20 items-center justify-center rounded-2xl bg-slate-800">
            <svg
              className="h-10 w-10 text-sky-400"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.6}
                d="M12 11c-3 0-5 1.8-5 4v3h10v-3c0-2.2-2-4-5-4zm0-1a3 3 0 100-6 3 3 0 000 6z"
              />
            </svg>
          </div>
          <p className="text-sm text-slate-400">Приложение заблокировано</p>
          <button
            type="button"
            onClick={tryBiometric}
            className="rounded-lg bg-sky-500 px-6 py-2.5 text-sm font-medium text-white"
          >
            Разблокировать
          </button>
          {pinAvailable && (
            <button
              type="button"
              onClick={() => setMethod("pin")}
              className="text-sm text-slate-400 hover:text-slate-200"
            >
              Ввести PIN-код
            </button>
          )}
        </>
      ) : (
        <>
          <p className="text-sm text-slate-400">
            {pinError ? "Неверный PIN-код" : "Введите PIN-код"}
          </p>
          <PinPad
            value={pin}
            onChange={(v) => {
              setPinError(false);
              setPin(v);
            }}
            dots={expectedLen.current ?? undefined}
            error={pinError}
          />
        </>
      )}
    </div>
  );
}

// Back-compat helpers (used by settings). Kept so existing imports don't break.
export function isAppLockEnabled(): boolean {
  return getLockMode() !== "off";
}

export function setAppLockEnabled(): void {
  // No-op: superseded by the lock-mode selector in settings.
}
