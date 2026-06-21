"use client";

import { useCallback, useEffect, useState } from "react";
import QRCode from "qrcode";
import { mutFetch } from "@/lib/auth";

/**
 * Authenticated desktop page that shows a QR code for logging a phone in.
 * The phone scans it (in the app's login screen) and is signed in without typing
 * credentials. The token is single-use and short-lived; it carries no session
 * secret (the backend relays the session server-side on redeem).
 */
export default function QrLoginPage() {
  const [qr, setQr] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const res = await mutFetch("/api/auth/qr-login/create", {
        method: "POST",
      });
      if (!res.ok) {
        setError("Не удалось создать QR-код. Войдите заново.");
        return;
      }
      const data = (await res.json()) as { token: string; expires_in: number };
      const url = `${window.location.origin}/auth/qr-redeem?t=${data.token}`;
      const img = await QRCode.toDataURL(url, { width: 260, margin: 1 });
      setQr(img);
      setSecondsLeft(data.expires_in);
    } catch {
      setError("Ошибка сети.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Countdown + auto-refresh shortly before the token expires.
  useEffect(() => {
    if (secondsLeft <= 0) return;
    const t = setInterval(() => {
      setSecondsLeft((s) => {
        if (s <= 1) {
          void refresh();
          return 0;
        }
        return s - 1;
      });
    }, 1000);
    return () => clearInterval(t);
  }, [secondsLeft, refresh]);

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-5 p-6 text-slate-100">
      <h1 className="text-xl font-semibold">Вход на телефоне по QR-коду</h1>
      <p className="max-w-sm text-center text-sm text-slate-400">
        Откройте приложение «Света» на телефоне, нажмите «Войти по QR-коду» и
        отсканируйте этот код. Вход выполнится без ввода пароля.
      </p>

      <div className="rounded-2xl bg-white p-4">
        {qr ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={qr} alt="QR-код для входа" width={260} height={260} />
        ) : (
          <div className="flex h-[260px] w-[260px] items-center justify-center text-slate-400">
            …
          </div>
        )}
      </div>

      {error ? (
        <p className="text-sm text-red-400">{error}</p>
      ) : (
        <p className="text-xs text-slate-500">
          Код обновится через {secondsLeft} с
        </p>
      )}

      <button
        onClick={() => void refresh()}
        className="rounded-lg border border-slate-700 px-5 py-2.5 text-sm font-medium text-slate-200"
      >
        Обновить код
      </button>
    </div>
  );
}
