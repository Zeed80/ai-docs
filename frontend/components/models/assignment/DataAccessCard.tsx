"use client";

/**
 * «Полный доступ моделей к данным» — решение оператора, а не настройка формы.
 *
 * Включённый флажок даёт всем моделям, в том числе облачным, чтение всех таблиц
 * базы через SQL-конвейер агента, кроме секретных. Включение — только через
 * подтверждение: в облако уходит схема базы и текст задачи, а модель видит
 * почту, память, задачи и всё остальное, что раньше было закрыто.
 */

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import { SectionCard } from "@/components/ui/primitives/SectionCard";

const API = getApiBaseUrl();
const URL = `${API}/api/providers/policy/data-access`;

type DataAccess = {
  sql_full_access: boolean;
  secret_tables: string[];
  secret_columns: Record<string, string[]>;
  granted_tables?: number | null;
};

const WARNING =
  "Включить полный доступ моделей к данным?\n\n" +
  "• Все модели, включая облачные, смогут читать через SQL все таблицы базы: " +
  "счета, документы, почту, чаты, память агента, задачи.\n" +
  "• Облачному провайдеру уходит схема базы и текст задачи; результаты запросов " +
  "показываются на рабочем столе.\n" +
  "• Запись по-прежнему запрещена; ключи, пароли и токены остаются закрыты.\n\n" +
  "Решение записывается в журнал. Ответственность за передачу данных — на вас.";

export function DataAccessCard() {
  const toast = useToast();
  const [state, setState] = useState<DataAccess | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    fetch(URL, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body: DataAccess | null) => {
        if (alive && body) setState(body);
      })
      .catch(() => {
        // Без карточки экран работает: это одна настройка, не весь раздел.
      });
    return () => {
      alive = false;
    };
  }, []);

  async function toggle(enabled: boolean) {
    if (enabled && !window.confirm(WARNING)) return;
    setBusy(true);
    try {
      const r = await fetch(URL, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ enabled, acknowledged: enabled }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        toast.error("Доступ не изменён", String(body.detail || r.status));
        return;
      }
      setState(body);
    } catch (e) {
      toast.error("Доступ не изменён", String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!state) return null;
  const hidden = [
    ...state.secret_tables,
    ...Object.entries(state.secret_columns).flatMap(([t, cols]) =>
      cols.map((c) => `${t}.${c}`),
    ),
  ];
  return (
    <SectionCard title="Доступ моделей к данным">
      <label className="flex min-w-0 cursor-pointer items-start gap-3">
        <input
          type="checkbox"
          className="mt-1 shrink-0"
          checked={state.sql_full_access}
          disabled={busy}
          onChange={(e) => void toggle(e.target.checked)}
          aria-describedby="data-access-hint"
        />
        <span className="min-w-0 text-sm text-slate-200">
          Полный доступ к данным для всех моделей, включая облачные
          <span
            id="data-access-hint"
            className="mt-1 block break-words text-xs text-slate-400"
          >
            {state.sql_full_access
              ? "Включено: модели читают все таблицы через SQL (только чтение). "
              : "Выключено: SQL видит 7 таблиц (документы, счета, контрагенты, " +
                "аномалии, согласования, пользователи), облачные модели к нему не допускаются. "}
            Схема всех таблиц — около 10 тыс. токенов в каждом запросе: нужна
            модель с большим контекстом. Всегда закрыто: {hidden.join(", ")}.
          </span>
        </span>
      </label>
    </SectionCard>
  );
}
