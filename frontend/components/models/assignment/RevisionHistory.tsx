"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/primitives/Button";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { tz } from "@/lib/user-time";

const API = getApiBaseUrl();

interface RevisionSummary {
  slot: string;
  old_model: string | null;
  new_model: string | null;
}

interface Revision {
  id: string;
  created_at: string;
  created_by: string;
  summary: RevisionSummary[];
  warnings_count: number;
}

/**
 * История назначений.
 *
 * Ревизии записывались всегда, но прочитать их было нечем — существовал
 * только откат по id. Поэтому «Откатить» относился лишь к последнему
 * изменению текущей вкладки и пропадал при перезагрузке страницы, а вопрос
 * «кто и когда сменил модель» оставался без ответа.
 */
export function RevisionHistory({
  onRolledBack,
}: {
  onRolledBack: () => void;
}) {
  const [items, setItems] = useState<Revision[] | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `${API}/api/providers/assignments/revisions?limit=20`,
        {
          credentials: "include",
        },
      );
      if (!r.ok) {
        setError(`История не загрузилась: сервер ответил ${r.status}`);
        return;
      }
      setItems(await r.json());
      setError(null);
    } catch {
      setError("История не загрузилась: нет связи с сервером");
    }
  }, []);

  useEffect(() => {
    if (open && items === null) void load();
  }, [open, items, load]);

  const rollback = async (id: string) => {
    setBusy(id);
    try {
      const r = await fetch(`${API}/api/providers/assignments/${id}/rollback`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      if (!r.ok) {
        setError(`Откат не выполнен: сервер ответил ${r.status}`);
        return;
      }
      await load();
      onRolledBack();
    } catch {
      setError("Откат не выполнен: нет связи с сервером");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-md border border-slate-700 bg-slate-900/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs text-slate-300 hover:bg-slate-800/50"
      >
        <span>История назначений</span>
        <span aria-hidden="true" className="text-slate-400">
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <div className="border-t border-slate-700 px-3 py-2">
          {error && (
            <p role="alert" className="mb-2 text-[11px] text-red-400">
              {error}
            </p>
          )}
          {items === null && (
            <p className="text-[11px] text-slate-400">Загрузка…</p>
          )}
          {items?.length === 0 && (
            <p className="text-[11px] text-slate-400">
              Изменений ещё не было — история появится после первого применения.
            </p>
          )}
          <ul className="space-y-2">
            {items?.map((rev) => (
              <li
                key={rev.id}
                className="flex items-start justify-between gap-3 border-b border-slate-800 pb-2 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-[11px] text-slate-400">
                    {new Date(rev.created_at).toLocaleString("ru-RU", {
                      timeZone: tz(),
                    })}{" "}
                    · {rev.created_by}
                    {rev.warnings_count > 0 && (
                      <span className="ml-1 text-amber-400">
                        · с предупреждениями ({rev.warnings_count})
                      </span>
                    )}
                  </p>
                  <ul className="mt-0.5 space-y-0.5">
                    {rev.summary.map((c, i) => (
                      <li
                        key={i}
                        className="truncate text-[11px] text-slate-300"
                      >
                        {c.slot}: {c.old_model ?? "—"} → {c.new_model ?? "—"}
                      </li>
                    ))}
                  </ul>
                </div>
                <Button
                  variant="secondary"
                  loading={busy === rev.id}
                  onClick={() => rollback(rev.id)}
                  className="shrink-0"
                >
                  Откатить
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
