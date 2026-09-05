"use client";

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import {
  btn,
  card,
  cardHeader as cardH,
} from "@/components/ui/primitives/tokens";

const API = getApiBaseUrl();
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;

/**
 * Полная цепочка моделей каждой задачи.
 *
 * Экран назначения показывает по слоту одну — назначенную — модель. Остальная
 * часть цепочки фолбэков невидима, поэтому мусор в ней может лежать годами:
 * так после перехода gemma4 → qwen3.8 ссылки на gemma4 остались у большинства
 * задач. Пока голова цепочки жива, это не проявляется; как только она
 * недоступна, каждая попытка фолбэка стоит запроса к несуществующей модели.
 */
interface ChainEntry {
  key: string;
  provider_model: string | null;
  provider: string | null;
  availability: "available" | "missing" | "unknown" | "not_in_catalog";
  is_primary: boolean;
}

interface ChainRow {
  task: string;
  models: ChainEntry[];
  dead: number;
}

const AVAIL_LABEL: Record<ChainEntry["availability"], string> = {
  available: "есть на узле",
  missing: "модели нет на узле",
  unknown: "узел не ответил — неизвестно",
  not_in_catalog: "ключа нет в каталоге",
};

export function RoutingChains() {
  const [rows, setRows] = useState<ChainRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/providers/routing-health`, {
        credentials: "include",
      });
      if (r.ok) setRows(await r.json());
    } catch {
      /* ignore */
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const prune = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch(`${API}/api/providers/routing-health/prune`, {
        method: "POST",
        headers: { ...(await csrfHeaders()) },
        credentials: "include",
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        const failed = Object.keys(d.failed ?? {}).length;
        setMsg(
          failed
            ? `Убрано звеньев: ${d.total ?? 0}; не удалось у задач: ${failed}`
            : `Убрано звеньев: ${d.total ?? 0}`,
        );
        await load();
      } else {
        setMsg(`Ошибка: ${d.detail || r.status}`);
      }
    } catch (e) {
      setMsg(`Ошибка: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  const dead = rows.reduce((n, r) => n + r.dead, 0);

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/40 p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-200">
            Цепочки моделей по задачам
          </h3>
          <p className="mt-1 text-xs text-slate-500">
            Выше видна только назначенная модель. Здесь — весь порядок
            фолбэков: именно по нему пойдёт запрос, если основная модель
            недоступна.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {msg && <span className="text-xs text-emerald-400">{msg}</span>}
          {dead > 0 && (
            <button
              className={`${btnSecondary} text-xs`}
              disabled={busy}
              onClick={prune}
              title="Убрать из цепочек модели, которых нет на узлах. Назначенную модель не трогает."
            >
              {busy ? "…" : `Убрать мёртвые (${dead})`}
            </button>
          )}
          <button
            className={`${btnSecondary} text-xs`}
            onClick={() => setOpen((o) => !o)}
          >
            {open ? "Свернуть" : "Показать"}
          </button>
        </div>
      </div>

      {loading ? (
        <p className="mt-3 text-xs text-slate-500">Загрузка…</p>
      ) : dead > 0 ? (
        <p className="mt-3 text-xs text-amber-400">
          Мёртвых звеньев: {dead}. Пока основная модель работает, это незаметно
          — но каждая попытка фолбэка на них стоит запроса впустую.
        </p>
      ) : (
        <p className="mt-3 text-xs text-emerald-400">
          Мёртвых звеньев нет.
        </p>
      )}

      {open && (
        <div className="mt-3 space-y-3">
          {rows.map((r) => (
            <div key={r.task} className="text-xs">
              <div className="font-mono text-slate-300">{r.task}</div>
              <ul className="mt-0.5 space-y-0.5">
                {r.models.map((m) => (
                  <li
                    key={m.key}
                    className={`flex items-center gap-2 ${
                      m.availability === "available"
                        ? "text-slate-400"
                        : m.availability === "unknown"
                          ? "text-slate-500"
                          : "text-amber-400"
                    }`}
                  >
                    <span className="w-3 shrink-0">
                      {m.is_primary ? "★" : "·"}
                    </span>
                    <span className="truncate font-mono">
                      {m.provider_model || m.key}
                    </span>
                    <span className="shrink-0 text-[11px] text-slate-600">
                      {AVAIL_LABEL[m.availability]}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
