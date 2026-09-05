"use client";

/**
 * Ф8 — «Здоровье почты».
 *
 * Каждый отказ в этой подсистеме тихий: ящик перестал синкаться, очередь
 * write-back растёт, у вложений есть строка и нет байтов. Ничего из этого
 * никуда не всплывает само — нужен экран, куда смотрят.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

interface FolderHealth {
  id: string;
  remote_name: string;
  local_folder: string | null;
  sync_enabled: boolean;
  last_sync_at: string | null;
  sync_error: string | null;
  uid_validity: number | null;
}

interface MailboxHealth {
  name: string;
  display_name: string | null;
  is_active: boolean;
  last_sync_at: string | null;
  sync_error: string | null;
  messages: number;
  unread: number;
  attachments_without_bytes: number;
  pending_sync_ops: number;
  failed_sync_ops: number;
  triage_mode: string;
  triaged: number;
  triage_corrections: number;
  folders: FolderHealth[];
}

/** Наши системные папки — куда можно отобразить папку сервера. */
const LOCAL_FOLDERS = [
  ["", "не синкать"],
  ["inbox", "Входящие"],
  ["sent", "Отправленные"],
  ["drafts", "Черновики"],
  ["trash", "Корзина"],
  ["spam", "Спам"],
  ["archive", "Архив"],
] as const;

const TRIAGE_LABEL: Record<string, string> = {
  off: "выключен",
  classify: "только классификация",
  full: "полный",
};

function ago(iso: string | null): string {
  if (!iso) return "никогда";
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} ч назад`;
  return `${Math.round(h / 24)} дн назад`;
}

/** Устаревший синк — это отказ, даже когда ошибки нет. */
function isStale(iso: string | null): boolean {
  if (!iso) return true;
  return Date.now() - new Date(iso).getTime() > 60 * 60 * 1000;
}

function Metric({
  label,
  value,
  bad,
}: {
  label: string;
  value: number;
  bad?: boolean;
}) {
  return (
    <div>
      <div
        className={`text-lg font-semibold ${bad && value > 0 ? "text-amber-400" : "text-slate-100"}`}
      >
        {value.toLocaleString("ru-RU")}
      </div>
      <div className="text-xs text-slate-400">{label}</div>
    </div>
  );
}

export function MailHealthSection() {
  const [rows, setRows] = useState<MailboxHealth[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  async function mapFolder(folderId: string, local: string) {
    setSaving(folderId);
    setError(null);
    try {
      const res = await mutFetch(`${API}/api/mailbox/folders/${folderId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          local_folder: local || null,
          sync_enabled: !!local,
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  const load = useCallback(() => {
    apiFetch(`${API}/api/mailbox/health`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => {
        setRows(Array.isArray(d) ? d : []);
        setError(null);
      })
      .catch((e) => setError(String(e.message ?? e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h2 className="text-lg font-semibold">Здоровье почты</h2>
          <p className="mt-1 text-sm text-slate-400">
            По каждому ящику: когда был синк, что сломалось, сколько операций
            ждёт отправки на сервер. Обновляется раз в минуту.
          </p>
        </div>
        <button
          onClick={load}
          className="px-3 py-1.5 text-sm bg-slate-700 hover:bg-slate-600 rounded"
        >
          Обновить
        </button>
      </div>

      {loading && <p className="text-sm text-slate-400">Загрузка…</p>}
      {error && (
        <p className="text-sm text-red-400">Не удалось получить статус: {error}</p>
      )}
      {!loading && !error && rows.length === 0 && (
        <p className="text-sm text-slate-400">Ни одного ящика не настроено.</p>
      )}

      <div className="space-y-3">
        {rows.map((m) => {
          const stale = m.is_active && isStale(m.last_sync_at);
          const broken = !!m.sync_error;
          return (
            <div
              key={m.name}
              className={`rounded border p-4 ${
                broken
                  ? "border-red-700 bg-red-950/30"
                  : stale
                    ? "border-amber-700 bg-amber-950/20"
                    : "border-slate-700 bg-slate-900/40"
              }`}
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="font-medium">
                    {m.display_name || m.name}
                    {!m.is_active && (
                      <span className="ml-2 text-xs text-slate-400">(отключён)</span>
                    )}
                  </div>
                  <div className="text-xs text-slate-400">
                    {m.name} · синк {ago(m.last_sync_at)} · разбор агентом:{" "}
                    {TRIAGE_LABEL[m.triage_mode] ?? m.triage_mode}
                  </div>
                </div>
                {m.folders.length > 0 && (
                  <button
                    onClick={() => setExpanded(expanded === m.name ? null : m.name)}
                    className="text-xs text-slate-300 hover:text-white shrink-0"
                  >
                    {expanded === m.name ? "Скрыть папки" : `Папки (${m.folders.length})`}
                  </button>
                )}
              </div>

              {broken && (
                <p className="mt-2 text-sm text-red-300 break-words">{m.sync_error}</p>
              )}
              {!broken && stale && (
                <p className="mt-2 text-sm text-amber-300">
                  Ошибки нет, но синка не было больше часа — проверьте, работает ли
                  планировщик.
                </p>
              )}

              <div className="mt-3 grid grid-cols-2 sm:grid-cols-5 gap-4">
                <Metric label="писем" value={m.messages} />
                <Metric label="непрочитанных" value={m.unread} />
                <Metric label="ждут отправки на сервер" value={m.pending_sync_ops} bad />
                <Metric label="операций с ошибкой" value={m.failed_sync_ops} bad />
                <Metric label="вложений без файла" value={m.attachments_without_bytes} bad />
              </div>

              {m.triaged > 0 && (
                <p className="mt-3 text-xs text-slate-400">
                  Разобрано агентом: {m.triaged.toLocaleString("ru-RU")} писем ·
                  человек исправил{" "}
                  <span
                    className={
                      m.triage_corrections / m.triaged > 0.2
                        ? "text-amber-400"
                        : "text-slate-300"
                    }
                  >
                    {m.triage_corrections} (
                    {Math.round((m.triage_corrections / m.triaged) * 100)}%)
                  </span>
                  {" — "}
                  это и есть ответ на вопрос, можно ли доверить ему ящик.
                </p>
              )}

              {expanded === m.name && (
                <div className="mt-3 border-t border-slate-700 pt-3 space-y-1">
                  <p className="pb-1 text-[11px] text-slate-400">
                    Папку, которую мы не узнали, можно назначить вручную:
                    провайдеры раскладывают почту по-своему, а несинкаемая папка
                    — это письма, которые есть на сервере и которых нет здесь.
                  </p>
                  {m.folders.map((f) => (
                    <div
                      key={f.id}
                      className="flex items-center justify-between gap-3 text-xs"
                    >
                      <span
                        className={`truncate ${f.sync_enabled ? "" : "text-slate-400"}`}
                      >
                        {f.remote_name}
                      </span>
                      <div className="flex shrink-0 items-center gap-2">
                        <span
                          className={f.sync_error ? "text-red-400" : "text-slate-400"}
                        >
                          {f.sync_error ? f.sync_error : ago(f.last_sync_at)}
                        </span>
                        <select
                          value={f.local_folder ?? ""}
                          disabled={saving === f.id}
                          onChange={(e) => mapFolder(f.id, e.target.value)}
                          className="rounded border border-slate-600 bg-slate-800 px-1 py-0.5 text-[11px] disabled:opacity-50"
                        >
                          {LOCAL_FOLDERS.map(([v, label]) => (
                            <option key={v} value={v}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
