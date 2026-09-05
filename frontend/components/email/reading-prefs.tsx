"use client";

/**
 * Ф1.4 — настройки чтения почты: показывать ли удалённые изображения.
 *
 * Блокировка по умолчанию защищает от трекинг-пикселя, но если единственный
 * способ не нажимать «Показать» на каждом письме — выключить защиту целиком,
 * её и выключают целиком. Поэтому здесь общий переключатель, а доверие
 * конкретному отправителю ставится прямо из письма.
 */

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

export function MailReadingPrefsCard() {
  const [always, setAlways] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch(`${API}/api/email/preferences`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAlways(!!d?.always_show_images))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  async function toggle(next: boolean) {
    setBusy(true);
    setError(null);
    const before = always;
    setAlways(next);
    try {
      const res = await mutFetch(`${API}/api/email/preferences`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ always_show_images: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      setAlways(before);
      setError(e instanceof Error ? e.message : "Не удалось сохранить");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
      <h2 className="text-lg font-semibold">Чтение писем</h2>
      <p className="mt-1 mb-4 text-sm text-slate-400">
        Удалённые изображения в письмах по умолчанию не загружаются: картинка
        размером в пиксель — обычный способ узнать, что письмо открыли, когда и
        с какого адреса.
      </p>
      <label className="flex items-start gap-2 text-sm text-slate-300 cursor-pointer">
        <input
          type="checkbox"
          checked={always}
          disabled={loading || busy}
          onChange={(e) => toggle(e.target.checked)}
          className="mt-0.5 rounded"
        />
        <span>
          Всегда показывать изображения
          <span className="block text-xs text-slate-400">
            Отдельным отправителям можно доверять точечно — кнопка «всегда для
            отправителя» есть в самом письме.
          </span>
        </span>
      </label>
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </section>
  );
}
