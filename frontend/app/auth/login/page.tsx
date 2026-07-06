"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { loginUrl } from "@/lib/auth";
import { isNative, scanQr } from "@/lib/native-bridge";
import {
  hasQuickLogin,
  pinLengthHint,
  quickLoginMethod,
  unlockBiometric,
  unlockPin,
} from "@/lib/quick-login";
import PinPad from "@/components/mobile/PinPad";

/** Extract a QR-login token from a scanned URL or a bare token string. */
function tokenFromScan(raw: string): { url?: string; token?: string } | null {
  const v = raw.trim();
  if (!v) return null;
  try {
    const u = new URL(v);
    const t = u.searchParams.get("t");
    if (t) return { url: v, token: t };
  } catch {
    /* not a URL */
  }
  // Bare token (no scheme, no spaces) → redeem on the current origin.
  if (/^[A-Za-z0-9_-]{16,}$/.test(v)) return { token: v };
  return null;
}

function Spinner() {
  return (
    <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin mx-auto mb-3" />
  );
}

function LoginScreen() {
  const params = useSearchParams();
  const [native, setNative] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  // Quick-login (biometric / PIN) state.
  const [quick, setQuick] = useState<"biometric" | "pin" | null>(null);
  const [pin, setPin] = useState("");
  const [pinError, setPinError] = useState(false);
  const [unlocking, setUnlocking] = useState(false);
  const bioTried = useRef(false);

  const next = params.get("next") ?? "/inbox";

  useEffect(() => {
    const isApp = isNative();
    setNative(isApp);
    if (!isApp) {
      // On the desktop/web, keep the original behaviour: go straight to SSO.
      window.location.href = loginUrl(next);
      return;
    }
    if (hasQuickLogin()) setQuick(quickLoginMethod());
  }, [next]);

  const onUnlocked = useCallback(() => {
    // Full navigation so the freshly set session cookie is picked up.
    window.location.href = next;
  }, [next]);

  const doBiometric = useCallback(async () => {
    setError(null);
    setUnlocking(true);
    try {
      await unlockBiometric();
      onUnlocked();
    } catch (e) {
      setError(String((e as Error).message || e));
      setUnlocking(false);
    }
  }, [onUnlocked]);

  // Auto-prompt the fingerprint dialog once when arriving with biometric set.
  useEffect(() => {
    if (quick === "biometric" && !bioTried.current) {
      bioTried.current = true;
      void doBiometric();
    }
  }, [quick, doBiometric]);

  // Verify the PIN once it reaches the configured length.
  useEffect(() => {
    if (quick !== "pin") return;
    const len = pinLengthHint();
    if (!len || pin.length < len) return;
    let cancelled = false;
    setUnlocking(true);
    void unlockPin(pin)
      .then(() => {
        if (!cancelled) onUnlocked();
      })
      .catch(() => {
        if (cancelled) return;
        setPinError(true);
        setUnlocking(false);
        setTimeout(() => {
          setPin("");
          setPinError(false);
        }, 600);
      });
    return () => {
      cancelled = true;
    };
  }, [pin, quick, onUnlocked]);

  async function loginByQr() {
    setError(null);
    setScanning(true);
    try {
      const raw = await scanQr();
      if (!raw) {
        setError("QR-код не распознан.");
        return;
      }
      const parsed = tokenFromScan(raw);
      if (!parsed) {
        setError("Это не QR-код для входа.");
        return;
      }
      window.location.href = parsed.url ?? `/auth/qr-redeem?t=${parsed.token}`;
    } finally {
      setScanning(false);
    }
  }

  function usePassword() {
    // Fall back to SSO for this launch without dropping the saved quick-login.
    setQuick(null);
    window.location.href = loginUrl(next);
  }

  // Web: blank while redirecting to SSO.
  if (native !== true) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50">
        <div className="text-center">
          <Spinner />
          <p className="text-sm text-slate-500">
            Перенаправление на страницу входа…
          </p>
        </div>
      </div>
    );
  }

  // Native app with a saved quick-login → biometric / PIN unlock.
  if (quick) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-7 bg-slate-900 p-6 text-slate-100">
        <div className="h-16 w-16 rounded-2xl bg-sky-500" />
        <h1 className="text-xl font-semibold">Вход в AI-DOCS</h1>

        {quick === "biometric" ? (
          <div className="flex flex-col items-center gap-5">
            <button
              onClick={doBiometric}
              disabled={unlocking}
              className="flex h-20 w-20 items-center justify-center rounded-full bg-slate-800 active:bg-slate-700 disabled:opacity-60"
              aria-label="Войти по отпечатку"
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
                  strokeWidth={1.5}
                  d="M12 11c-3 0-5 1.8-5 4v3h10v-3c0-2.2-2-4-5-4zm0-1a3 3 0 100-6 3 3 0 000 6z"
                />
              </svg>
            </button>
            <p className="text-sm text-slate-400">
              {unlocking ? "Проверка…" : "Приложите палец для входа"}
            </p>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-5">
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
          </div>
        )}

        {error && <p className="text-sm text-red-400">{error}</p>}

        <button
          onClick={usePassword}
          className="text-sm text-slate-400 hover:text-slate-200"
        >
          Войти паролем
        </button>
      </div>
    );
  }

  // Native app without quick-login: choose between SSO and QR login.
  return (
    <div className="min-h-screen flex flex-col items-center justify-center gap-5 bg-slate-900 p-6 text-slate-100">
      <div className="h-16 w-16 rounded-2xl bg-sky-500" />
      <h1 className="text-xl font-semibold">Вход в AI-DOCS</h1>
      <p className="max-w-xs text-center text-sm text-slate-400">
        Войдите через сервер или отсканируйте QR-код входа с уже открытого
        AI-DOCS на компьютере.
      </p>

      <div className="flex w-full max-w-xs flex-col gap-3">
        <button
          onClick={loginByQr}
          disabled={scanning}
          className="rounded-lg bg-sky-500 py-3 text-sm font-medium text-white disabled:opacity-60"
        >
          {scanning ? "Сканирование…" : "Войти по QR-коду"}
        </button>
        <button
          onClick={() => (window.location.href = loginUrl(next))}
          className="rounded-lg border border-slate-700 py-3 text-sm font-medium text-slate-200"
        >
          Войти через сервер
        </button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center bg-slate-50">
          <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
        </div>
      }
    >
      <LoginScreen />
    </Suspense>
  );
}
