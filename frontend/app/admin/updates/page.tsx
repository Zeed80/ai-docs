"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();
const BASE = `${API}/api/admin/updates`;

interface Job {
  status: "requested" | "running" | "done" | "error";
  mode?: string;
  target?: string | null;
  planned_hops?: string[];
  from_version?: string | null;
  requested_by?: string;
  current_step?: string | null;
  log_tail?: string;
  error?: string | null;
}

interface AuthentikInfo {
  current_version: string | null;
  current_minor: string | null;
  latest_minor: string;
  remaining: string[];
  next_hop: string | null;
  up_to_date: boolean;
  ladder: string[];
  job: Job | null;
}

function Banner({
  kind,
  children,
}: {
  kind: "ok" | "err" | "info";
  children: React.ReactNode;
}) {
  const cls =
    kind === "ok"
      ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
      : kind === "err"
        ? "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
        : "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200";
  return (
    <div className={`rounded-md px-3 py-2 text-sm ${cls}`}>{children}</div>
  );
}

function UpdatesContent() {
  const [info, setInfo] = useState<AuthentikInfo | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${BASE}/authentik`, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d: AuthentikInfo = await r.json();
      setInfo(d);
      // Keep polling while a job is in flight.
      if (
        d.job &&
        (d.job.status === "requested" || d.job.status === "running")
      ) {
        pollRef.current = setTimeout(() => void load(), 3000);
      }
    } catch (e) {
      setMsg({ kind: "err", text: `Не удалось загрузить статус: ${e}` });
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [load]);

  async function requestUpdate(mode: "next" | "latest", label: string) {
    if (
      !window.confirm(
        `Запросить обновление Authentik (${label})?\n\n` +
          `На каждом шаге SSO кратко перезапускается — выполняйте в окно ` +
          `обслуживания. Перед каждым шагом снимается полный бэкап; при сбое ` +
          `выполняется автоматический откат.\n\n` +
          `Выполнит host-агент (systemd-таймер update-agent). Продолжить?`,
      )
    )
      return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch(`${BASE}/authentik/request`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ mode }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail ?? `HTTP ${r.status}`);
      setMsg({ kind: "ok", text: d.note ?? "Заявка поставлена." });
      await load();
    } catch (e) {
      setMsg({ kind: "err", text: `${e}` });
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    try {
      const r = await fetch(`${BASE}/authentik/cancel`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail ?? `HTTP ${r.status}`);
      setMsg({ kind: "ok", text: "Заявка отменена." });
      await load();
    } catch (e) {
      setMsg({ kind: "err", text: `${e}` });
    } finally {
      setBusy(false);
    }
  }

  if (!info) return <p className="text-sm text-muted-foreground">Загрузка…</p>;

  const job = info.job;
  const jobActive =
    !!job && (job.status === "requested" || job.status === "running");

  return (
    <div className="space-y-5 max-w-2xl">
      <section className="space-y-3">
        <h2 className="text-base font-semibold">Authentik (SSO)</h2>
        <div className="rounded-lg border border-border p-4 space-y-2 text-sm">
          <div className="flex justify-between">
            <span className="text-muted-foreground">Текущая версия</span>
            <span className="font-mono">
              {info.current_version ?? "неизвестно"}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">Последняя доступная</span>
            <span className="font-mono">{info.latest_minor}</span>
          </div>
          {info.up_to_date ? (
            <Banner kind="ok">Установлена последняя известная версия.</Banner>
          ) : (
            <div className="pt-1">
              <p className="text-xs text-muted-foreground mb-1">
                Обновление идёт по одной мажорной версии за раз (пропуск версий
                Authentik не поддерживает). Осталось пройти:
              </p>
              <div className="flex flex-wrap gap-1">
                {info.remaining.map((v) => (
                  <span
                    key={v}
                    className="font-mono text-xs rounded bg-muted px-1.5 py-0.5"
                  >
                    {v}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {!info.up_to_date && (
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() =>
                requestUpdate("next", `следующая: ${info.next_hop}`)
              }
              disabled={busy || jobActive}
              className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            >
              Обновить на следующую ({info.next_hop})
            </button>
            <button
              onClick={() =>
                requestUpdate("latest", `до последней: ${info.latest_minor}`)
              }
              disabled={busy || jobActive}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              Обновить до последней ({info.latest_minor})
            </button>
          </div>
        )}

        <p className="text-xs text-muted-foreground">
          Выполняет host-агент (systemd-таймер <code>update-agent</code>). Если
          он не установлен, заявка не выполнится — см.{" "}
          <code>infra/installer/update-agent.README</code> или запустите{" "}
          <code>upgrade-authentik.sh</code> вручную.
        </p>
      </section>

      {msg && <Banner kind={msg.kind}>{msg.text}</Banner>}

      {job && (
        <section className="space-y-2">
          <h3 className="text-sm font-medium">Текущая операция</h3>
          <div className="rounded-lg border border-border p-4 space-y-2 text-sm">
            <div className="flex items-center gap-2">
              {jobActive && (
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
              )}
              <span className="font-medium">
                {job.status === "requested" && "Ожидает host-агента…"}
                {job.status === "running" &&
                  `Выполняется: ${job.current_step ?? "…"}`}
                {job.status === "done" && "Готово ✓"}
                {job.status === "error" && "Ошибка"}
              </span>
            </div>
            {job.planned_hops && (
              <p className="text-xs text-muted-foreground">
                План: {job.planned_hops.join(" → ")}
              </p>
            )}
            {job.error && <Banner kind="err">{job.error}</Banner>}
            {job.log_tail && (
              <pre className="max-h-64 overflow-auto rounded bg-muted/50 p-2 text-[11px] leading-snug whitespace-pre-wrap">
                {job.log_tail}
              </pre>
            )}
            {job.status === "requested" && (
              <button
                onClick={cancel}
                disabled={busy}
                className="rounded border border-border px-3 py-1 text-xs hover:bg-accent disabled:opacity-50"
              >
                Отменить заявку
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

export default function AdminUpdatesPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <UpdatesContent />
    </ProtectedRoute>
  );
}
