"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useCallback, useEffect, useState } from "react";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

type Status = "proposed" | "active" | "stale" | "archived" | "rejected" | "superseded";

interface LearnedProcedure {
  id: string;
  name: string;
  description: string;
  text: string;
  status: Status;
  origin: "agent" | "owner";
  reason: string | null;
  source_work_order_id: string | null;
  proposed_by: string;
  decided_by: string | null;
  decided_at: string | null;
  pinned: boolean;
  uses: number;
  last_used_at: string | null;
  created_at: string;
  current_text: string | null;
}

const STATUS_LABELS: Record<Status, string> = {
  proposed: "Ждёт решения",
  active: "Действует",
  stale: "Давно не читалась",
  archived: "В архиве",
  rejected: "Отклонена",
  superseded: "Заменена новой версией",
};

const STATUS_COLORS: Record<Status, string> = {
  proposed: "bg-amber-900/30 text-amber-400 border-amber-700/40",
  active: "bg-green-900/30 text-green-400 border-green-700/40",
  stale: "bg-slate-800 text-amber-300 border-amber-800/40",
  archived: "bg-slate-800 text-slate-400 border-slate-700",
  rejected: "bg-slate-800 text-slate-500 border-slate-700",
  superseded: "bg-slate-800 text-slate-500 border-slate-700",
};

const ORDER: Status[] = ["proposed", "active", "stale", "archived", "rejected", "superseded"];

function when(value: string | null): string {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}

export default function ProceduresSettingsPage() {
  const [rows, setRows] = useState<LearnedProcedure[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [showHistory, setShowHistory] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch(`${API}/api/agent/procedures-registry`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setRows((await resp.json()) as LearnedProcedure[]);
    } catch (e) {
      setError(`Не удалось загрузить процедуры: ${e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const post = async (id: string, path: string, body?: unknown) => {
    setBusyId(id);
    setError(null);
    try {
      const resp = await mutFetch(`${API}/api/agent/procedures-registry/${id}/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail ?? `HTTP ${resp.status}`);
      }
      setEditing((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      await load();
    } catch (e) {
      setError(`Операция не выполнена: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusyId(null);
    }
  };

  const visible = rows
    .filter((r) => showHistory || !["rejected", "superseded"].includes(r.status))
    .sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status));
  const waiting = rows.filter((r) => r.status === "proposed").length;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Процедуры агента</h1>
        <p className="mt-1 text-sm text-slate-400">
          Порядок работы, который агент читает перед задачей по теме. Встроенные процедуры
          меняются вместе с программой; здесь — те, что агент предложил сам (после вашего
          «запомни» или после многошаговой работы), и новые версии встроенных. Предложение
          вступает в силу только после вашего решения. Процедура, которую 14 дней не читали,
          помечается, через 30 — уходит в архив; закреплённые не трогаются.
        </p>
      </div>

      {error && (
        <div className="rounded-md border border-red-700/40 bg-red-900/20 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 text-sm text-slate-400">
        <span>Ждут решения: {waiting}</span>
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={showHistory}
            onChange={(e) => setShowHistory(e.target.checked)}
          />
          Показать отклонённые и прежние версии
        </label>
      </div>

      {loading ? (
        <div className="text-sm text-slate-400">Загрузка…</div>
      ) : visible.length === 0 ? (
        <div className="rounded-md border border-slate-700 bg-slate-900/50 px-4 py-6 text-sm text-slate-400">
          Процедур от агента пока нет. Скажите в чате «запомни, как…» — агент предложит
          процедуру, и она появится здесь.
        </div>
      ) : (
        <div className="space-y-3">
          {visible.map((p) => {
            const draft = editing[p.id];
            const busy = busyId === p.id;
            return (
              <section
                key={p.id}
                aria-label={`Процедура ${p.name}`}
                className="min-w-0 rounded-lg border border-slate-700 bg-slate-900/50 p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm text-slate-200">{p.name}</span>
                  <span className={`rounded border px-1.5 py-0.5 text-[11px] ${STATUS_COLORS[p.status]}`}>
                    {STATUS_LABELS[p.status]}
                  </span>
                  {p.pinned && (
                    <span className="rounded border border-blue-700/40 bg-blue-900/30 px-1.5 py-0.5 text-[11px] text-blue-300">
                      Закреплена
                    </span>
                  )}
                  <span className="text-xs text-slate-500">
                    {p.origin === "agent" ? "предложил агент" : "предложил человек"} · {when(p.created_at)}
                  </span>
                </div>
                <p className="mt-1 text-sm text-slate-300">{p.description}</p>
                {p.reason && <p className="mt-1 text-xs text-slate-500">Почему: {p.reason}</p>}

                {p.status === "proposed" && p.current_text && draft === undefined && (
                  <div className="mt-3 grid min-w-0 gap-2 md:grid-cols-2">
                    <div className="min-w-0">
                      <div className="mb-1 text-xs text-slate-500">Сейчас действует</div>
                      <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded border border-slate-700 bg-slate-950 p-2 text-xs text-slate-400">
                        {p.current_text}
                      </pre>
                    </div>
                    <div className="min-w-0">
                      <div className="mb-1 text-xs text-slate-500">Предложено</div>
                      <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded border border-amber-800/50 bg-slate-950 p-2 text-xs text-slate-200">
                        {p.text}
                      </pre>
                    </div>
                  </div>
                )}
                {(!p.current_text || p.status !== "proposed") && draft === undefined && (
                  <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded border border-slate-700 bg-slate-950 p-2 text-xs text-slate-300">
                    {p.text}
                  </pre>
                )}
                {draft !== undefined && (
                  <textarea
                    aria-label="Текст процедуры"
                    value={draft}
                    onChange={(e) => setEditing((prev) => ({ ...prev, [p.id]: e.target.value }))}
                    rows={10}
                    className="mt-3 w-full rounded border border-slate-600 bg-slate-950 p-2 font-mono text-xs text-slate-100 focus:border-blue-500 focus:outline-none"
                  />
                )}

                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                  {p.status !== "proposed" && (
                    <span>
                      Прочитана {p.uses} раз · последний раз {when(p.last_used_at)}
                    </span>
                  )}
                  {p.decided_by && <span>· решение: {p.decided_by}</span>}
                </div>

                <div className="mt-3 flex flex-wrap gap-2">
                  {p.status === "proposed" && (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          post(p.id, "decide", draft === undefined ? { approve: true } : { approve: true, text: draft })
                        }
                        className="rounded bg-green-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-600 disabled:opacity-50"
                      >
                        {draft === undefined ? "Одобрить" : "Одобрить с правкой"}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          setEditing((prev) => {
                            const next = { ...prev };
                            if (draft === undefined) next[p.id] = p.text;
                            else delete next[p.id];
                            return next;
                          })
                        }
                        className="rounded border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:border-blue-500"
                      >
                        {draft === undefined ? "Править" : "Отменить правку"}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => post(p.id, "decide", { approve: false })}
                        className="rounded border border-red-800/60 px-3 py-1.5 text-xs text-red-300 hover:bg-red-900/30 disabled:opacity-50"
                      >
                        Отклонить
                      </button>
                    </>
                  )}
                  {(p.status === "active" || p.status === "stale") && (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => post(p.id, "pin", { pinned: !p.pinned })}
                        className="rounded border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:border-blue-500 disabled:opacity-50"
                      >
                        {p.pinned ? "Открепить" : "Закрепить"}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => post(p.id, "archive")}
                        className="rounded border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:border-blue-500 disabled:opacity-50"
                      >
                        В архив
                      </button>
                    </>
                  )}
                  {p.status === "archived" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => post(p.id, "restore")}
                      className="rounded border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:border-blue-500 disabled:opacity-50"
                    >
                      Восстановить
                    </button>
                  )}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
