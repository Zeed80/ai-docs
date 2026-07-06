"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { biometricAvailable, isNative } from "@/lib/native-bridge";
import {
  cryptoSupported,
  enableBiometric,
  hasQuickLogin,
} from "@/lib/quick-login";

const DISMISS_KEY = "quick_login_prompted";

/**
 * One-time nudge (native shell) offering to turn on biometric/PIN quick-login
 * after the user has signed in with the password. Shown once; "Позже"/enabling
 * both mark it handled so it never nags again. Full management lives in settings.
 */
export function QuickLoginPrompt() {
  const router = useRouter();
  const [show, setShow] = useState(false);
  const [bioOk, setBioOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isNative()) return;
    if (hasQuickLogin()) return;
    if (localStorage.getItem(DISMISS_KEY)) return;
    if (!cryptoSupported()) return;
    void biometricAvailable().then((ok) => {
      setBioOk(ok);
      setShow(true);
    });
  }, []);

  function dismiss() {
    localStorage.setItem(DISMISS_KEY, "1");
    setShow(false);
  }

  async function enableBio() {
    setError(null);
    setBusy(true);
    try {
      await enableBiometric();
      dismiss();
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  function goToSettings() {
    localStorage.setItem(DISMISS_KEY, "1");
    setShow(false);
    router.push("/settings/notifications");
  }

  if (!show) return null;

  return (
    <div className="fixed inset-x-0 bottom-0 z-[90] p-3">
      <div
        className="mx-auto max-w-md rounded-xl border border-slate-700 bg-slate-900 p-4 shadow-xl"
        style={{ marginBottom: "calc(env(safe-area-inset-bottom) + 4.5rem)" }}
      >
        <p className="text-sm font-medium text-slate-100">Быстрый вход</p>
        <p className="mt-1 text-xs text-slate-400">
          Входить по отпечатку или PIN вместо пароля при каждом запуске?
        </p>
        {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {bioOk && (
            <button
              onClick={enableBio}
              disabled={busy}
              className="rounded-lg bg-sky-500 px-4 py-2 text-xs font-medium text-white disabled:opacity-60"
            >
              {busy ? "Включаю…" : "По отпечатку"}
            </button>
          )}
          <button
            onClick={goToSettings}
            className="rounded-lg border border-slate-700 px-4 py-2 text-xs font-medium text-slate-200"
          >
            Настроить PIN
          </button>
          <button
            onClick={dismiss}
            className="ml-auto text-xs text-slate-500 hover:text-slate-300"
          >
            Позже
          </button>
        </div>
      </div>
    </div>
  );
}
