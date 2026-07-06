"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import PinPad from "@/components/mobile/PinPad";
import { isNative } from "@/lib/native-bridge";
import {
  hasQuickLogin,
  pinLengthHint,
  quickLoginMethod,
  unlockBiometric,
  unlockPin,
  wasUnlockedThisSession,
} from "@/lib/quick-login";

const LOCK_TIMEOUT_MS = 2 * 60 * 1000; // re-lock after 2 min in background

/**
 * Lock overlay for the native shell when quick-login is enrolled. It locks the
 * already-open session on cold start and after the app has been backgrounded
 * past the timeout, and requires a fingerprint or PIN to continue. Unlocking
 * redeems the device credential, so it also refreshes the session. If quick-
 * login isn't set up, this renders nothing (the login screen handles auth).
 */
export function BiometricGate() {
  const [checking, setChecking] = useState(true);
  const [locked, setLocked] = useState(false);
  const [method, setMethod] = useState<"biometric" | "pin">("biometric");
  const [pin, setPin] = useState("");
  const [pinError, setPinError] = useState(false);
  const [unlocking, setUnlocking] = useState(false);
  const bioTried = useRef(false);

  useEffect(() => {
    if (!isNative()) {
      setChecking(false);
      return;
    }
    // Lock on a cold start into an already-authenticated session, but not right
    // after the login screen already unlocked us this WebView session.
    if (hasQuickLogin() && !wasUnlockedThisSession()) {
      setMethod(quickLoginMethod() === "pin" ? "pin" : "biometric");
      setLocked(true);
    }
    setChecking(false);
  }, []);

  const doBiometric = useCallback(async () => {
    setUnlocking(true);
    try {
      await unlockBiometric();
      setLocked(false);
    } catch {
      /* stay locked; the user can retry or use the password on the login page */
    } finally {
      setUnlocking(false);
    }
  }, []);

  // Auto-prompt biometrics when freshly locked.
  useEffect(() => {
    if (!checking && locked && method === "biometric" && !bioTried.current) {
      bioTried.current = true;
      void doBiometric();
    }
  }, [checking, locked, method, doBiometric]);

  // Verify the PIN once it reaches the configured length.
  useEffect(() => {
    if (!locked || method !== "pin") return;
    const len = pinLengthHint();
    if (!len || pin.length < len) return;
    let cancelled = false;
    setUnlocking(true);
    void unlockPin(pin)
      .then(() => !cancelled && setLocked(false))
      .catch(() => {
        if (cancelled) return;
        setPinError(true);
        setTimeout(() => {
          setPin("");
          setPinError(false);
        }, 600);
      })
      .finally(() => !cancelled && setUnlocking(false));
    return () => {
      cancelled = true;
    };
  }, [pin, locked, method]);

  // Re-lock when returning from the background after the timeout.
  useEffect(() => {
    if (checking || !isNative()) return;
    let hiddenAt = 0;
    function onVisibility() {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else if (
        hiddenAt &&
        Date.now() - hiddenAt > LOCK_TIMEOUT_MS &&
        hasQuickLogin()
      ) {
        bioTried.current = false;
        setPin("");
        setMethod(quickLoginMethod() === "pin" ? "pin" : "biometric");
        setLocked(true);
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
          <button
            onClick={doBiometric}
            disabled={unlocking}
            aria-label="Разблокировать по отпечатку"
            className="flex h-20 w-20 items-center justify-center rounded-2xl bg-slate-800 active:bg-slate-700 disabled:opacity-60"
          >
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
          </button>
          <p className="text-sm text-slate-400">
            {unlocking ? "Проверка…" : "Приложите палец для входа"}
          </p>
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
            dots={pinLengthHint() ?? undefined}
            error={pinError}
            disabled={unlocking}
          />
        </>
      )}
    </div>
  );
}
