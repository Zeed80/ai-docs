"use client";

/**
 * Тарифы моделей и расход по квитанциям (T4).
 *
 * Цена вызова фиксируется в его квитанции по тарифу, действовавшему в момент
 * вызова: новая версия не переписывает прошлое, задним числом не вводится.
 * Ноль — только у тарифа «бесплатно»; без тарифа расход «неизвестен».
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import { SectionCard } from "@/components/ui/primitives/SectionCard";
import { btn, input } from "@/components/ui/primitives/tokens";
import { detailText } from "@/lib/models/format";
import { providerLabel } from "@/lib/models/labels";

const API = getApiBaseUrl();

type Tariff = {
  id: string;
  provider: string;
  model: string;
  currency: string;
  input_per_1m: string | null;
  output_per_1m: string | null;
  free: boolean;
  source: string;
  effective_from: string;
  created_by: string | null;
};

type ReceiptCost = {
  provider: string;
  model: string | null;
  currency: string;
  calls: number;
  priced_calls: number;
  amount: string;
  unknown: Record<string, number>;
};

const UNKNOWN_REASON: Record<string, string> = {
  tariff_unavailable: "нет тарифа",
  tokens_unknown: "токены неизвестны",
};

const EMPTY = {
  provider: "",
  model: "",
  currency: "USD",
  input_per_1m: "",
  output_per_1m: "",
  free: false,
  source: "",
};

export function TariffsCard() {
  const toast = useToast();
  const [tariffs, setTariffs] = useState<Tariff[] | null>(null);
  const [costs, setCosts] = useState<ReceiptCost[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [t, c] = await Promise.all([
        fetch(`${API}/api/providers/tariffs`, { credentials: "include" }),
        fetch(`${API}/api/providers/cost-report`, { credentials: "include" }),
      ]);
      if (t.ok) setTariffs(await t.json());
      if (c.ok) setCosts(((await c.json()).receipts as ReceiptCost[]) ?? []);
    } catch {
      // Без карточки вкладка работает: тарифы — отдельная настройка.
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await fetch(`${API}/api/providers/tariffs`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          provider: form.provider.trim(),
          model: form.model.trim(),
          currency: form.currency.trim(),
          input_per_1m: form.free ? null : form.input_per_1m.trim() || null,
          output_per_1m: form.free ? null : form.output_per_1m.trim() || null,
          free: form.free,
          source: form.source.trim(),
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        toast.error("Тариф не добавлен", detailText(body) || String(r.status));
        return;
      }
      setForm(EMPTY);
      await load();
    } catch (err) {
      toast.error("Тариф не добавлен", String(err));
    } finally {
      setBusy(false);
    }
  }

  if (tariffs === null) return null;
  const set =
    (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((f) => ({
        ...f,
        [k]: k === "free" ? e.target.checked : e.target.value,
      }));

  return (
    <SectionCard title="Тарифы и расход">
      <p className="mb-3 break-words text-xs text-slate-400">
        Цена вызова записывается в его квитанцию по тарифу, действовавшему в
        момент вызова. Новая версия действует с момента добавления и прошлые
        вызовы не пересчитывает. Без тарифа расход — «неизвестно», ноль — только
        у тарифа «бесплатно».
      </p>

      {costs.length > 0 && (
        <div className="mb-4 min-w-0 overflow-x-auto">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1 pr-3">Модель</th>
                <th className="py-1 pr-3">Вызовов</th>
                <th className="py-1 pr-3">Сумма</th>
                <th className="py-1">Без цены</th>
              </tr>
            </thead>
            <tbody>
              {costs.map((c) => (
                <tr
                  key={`${c.provider}|${c.model}|${c.currency}`}
                  className="border-t border-slate-800"
                >
                  <td className="break-all py-1 pr-3">
                    {providerLabel(c.provider)} · {c.model ?? "—"}
                  </td>
                  <td className="py-1 pr-3">{c.calls}</td>
                  <td className="py-1 pr-3">
                    {c.priced_calls
                      ? `${c.amount} ${c.currency}`
                      : "неизвестно"}
                  </td>
                  <td className="py-1">
                    {Object.entries(c.unknown)
                      .map(
                        ([reason, n]) =>
                          `${UNKNOWN_REASON[reason] ?? reason}: ${n}`,
                      )
                      .join(", ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ul
        className="mb-4 space-y-1 text-xs text-slate-300"
        aria-label="Версии тарифов"
      >
        {tariffs.length === 0 && (
          <li className="text-slate-500">Тарифов пока нет.</li>
        )}
        {tariffs.map((t) => (
          <li key={t.id} className="break-words">
            {providerLabel(t.provider)} · {t.model}:{" "}
            {t.free
              ? "бесплатно"
              : `${t.input_per_1m} / ${t.output_per_1m} ${t.currency} за 1 млн токенов (вход / выход)`}{" "}
            <span className="text-slate-500">
              с {new Date(t.effective_from).toLocaleString("ru-RU")} ·{" "}
              {t.source}
            </span>
          </li>
        ))}
      </ul>

      <form
        onSubmit={submit}
        className="flex flex-wrap items-end gap-2"
        aria-label="Новая версия тарифа"
      >
        <input
          className={`${input} min-w-0 flex-1 basis-32`}
          placeholder="Провайдер (anthropic)"
          value={form.provider}
          onChange={set("provider")}
          required
          aria-label="Провайдер"
        />
        <input
          className={`${input} min-w-0 flex-1 basis-40`}
          placeholder="Модель"
          value={form.model}
          onChange={set("model")}
          required
          aria-label="Модель"
        />
        <input
          className={`${input} w-20 min-w-0`}
          placeholder="USD"
          value={form.currency}
          onChange={set("currency")}
          required
          aria-label="Валюта"
        />
        <input
          className={`${input} min-w-0 flex-1 basis-24`}
          placeholder="Вход за 1 млн"
          value={form.input_per_1m}
          onChange={set("input_per_1m")}
          disabled={form.free}
          inputMode="decimal"
          aria-label="Цена входа за 1 млн токенов"
        />
        <input
          className={`${input} min-w-0 flex-1 basis-24`}
          placeholder="Выход за 1 млн"
          value={form.output_per_1m}
          onChange={set("output_per_1m")}
          disabled={form.free}
          inputMode="decimal"
          aria-label="Цена выхода за 1 млн токенов"
        />
        <input
          className={`${input} min-w-0 flex-1 basis-40`}
          placeholder="Источник цены"
          value={form.source}
          onChange={set("source")}
          required
          aria-label="Источник цены"
        />
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" checked={form.free} onChange={set("free")} />
          Бесплатно
        </label>
        <button
          type="submit"
          disabled={busy}
          className={`${btn} bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-50`}
        >
          Добавить версию
        </button>
      </form>
    </SectionCard>
  );
}
