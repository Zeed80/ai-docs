"use client";

import { useCallback, useEffect, useState } from "react";
import {
  biometricAvailable,
  biometricVerify,
  isNative,
} from "@/lib/native-bridge";

const LOCK_PREF_KEY = "app_lock_enabled";
const LOCK_TIMEOUT_MS = 2 * 60 * 1000; // re-lock after 2 min in background

/**
 * Biometric (fingerprint/face/PIN) app-lock overlay for the native shell.
 * Locks on cold start and after the app has been backgrounded past the timeout.
 * Pure local gate over the existing WebView session — not server authentication.
 */
export function BiometricGate() {
  const [enabled, setEnabled] = useState(false);
  const [locked, setLocked] = useState(false);
  const [checking, setChecking] = useState(true);

  // Decide whether the lock applies on this device.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isNative()) {
        setChecking(false);
        return;
      }
      const pref = localStorage.getItem(LOCK_PREF_KEY);
      const wantLock = pref === null ? true : pref === "true"; // default on
      const available = wantLock && (await biometricAvailable());
      if (cancelled) return;
      setEnabled(available);
      setLocked(available);
      setChecking(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const unlock = useCallback(async () => {
    const ok = await biometricVerify("Разблокировать AI-DOCS");
    if (ok) setLocked(false);
  }, []);

  // Auto-prompt when freshly locked.
  useEffect(() => {
    if (enabled && locked) void unlock();
  }, [enabled, locked, unlock]);

  // Re-lock when returning from the background after the timeout.
  useEffect(() => {
    if (!enabled) return;
    let hiddenAt = 0;
    function onVisibility() {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else if (hiddenAt && Date.now() - hiddenAt > LOCK_TIMEOUT_MS) {
        setLocked(true);
      }
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [enabled]);

  if (checking || !enabled || !locked) return null;

  return (
    <div className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-6 bg-slate-950 text-slate-100">
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
        onClick={unlock}
        className="rounded-lg bg-sky-500 px-6 py-2.5 text-sm font-medium text-white"
      >
        Разблокировать
      </button>
    </div>
  );
}

export function isAppLockEnabled(): boolean {
  if (typeof window === "undefined") return true;
  const pref = localStorage.getItem(LOCK_PREF_KEY);
  return pref === null ? true : pref === "true";
}

export function setAppLockEnabled(value: boolean): void {
  if (typeof window !== "undefined")
    localStorage.setItem(LOCK_PREF_KEY, String(value));
}
