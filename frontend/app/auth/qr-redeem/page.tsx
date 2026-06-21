"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";

/**
 * Public QR-login redeem page. Reads ?t=<token>, exchanges it for a session
 * cookie via the backend, then enters the app. Reached either by the mobile app
 * (after scanning a desktop QR) or by opening the QR URL directly.
 */
function Redeem() {
  const params = useSearchParams();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = params.get("t");
    if (!token) {
      setError("Нет токена входа в ссылке.");
      return;
    }
    (async () => {
      try {
        const res = await fetch("/api/auth/qr-login/redeem", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setError(body?.detail ?? "Не удалось войти по QR-коду.");
          return;
        }
        // Session cookie is set — go to the app.
        window.location.replace("/inbox");
      } catch {
        setError("Ошибка сети. Повторите попытку.");
      }
    })();
  }, [params, router]);

  return (
    <div className="min-h-screen flex flex-col items-center justify-center gap-4 bg-slate-900 p-6 text-slate-100">
      {!error ? (
        <>
          <div className="w-8 h-8 border-2 border-sky-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-slate-400">Вход по QR-коду…</p>
        </>
      ) : (
        <>
          <p className="max-w-xs text-center text-sm text-red-400">{error}</p>
          <button
            onClick={() => router.replace("/auth/login")}
            className="rounded-lg bg-sky-500 px-5 py-2.5 text-sm font-medium text-white"
          >
            К входу
          </button>
        </>
      )}
    </div>
  );
}

export default function QrRedeemPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-slate-900" />}>
      <Redeem />
    </Suspense>
  );
}
