"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { loginUrl } from "@/lib/auth";
import { isNative, scanQr } from "@/lib/native-bridge";

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

  const next = params.get("next") ?? "/inbox";

  useEffect(() => {
    const isApp = isNative();
    setNative(isApp);
    // On the desktop/web, keep the original behaviour: go straight to SSO.
    if (!isApp) window.location.href = loginUrl(next);
  }, [next]);

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
      // A full URL → let the redeem page handle it (works across origins);
      // a bare token → redeem on the current server.
      window.location.href = parsed.url ?? `/auth/qr-redeem?t=${parsed.token}`;
    } finally {
      setScanning(false);
    }
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

  // Native app: choose between SSO and QR login.
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
