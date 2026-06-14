"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();
const BASE = `${API}/api/admin/maintenance`;

interface BackupInfo {
  name: string;
  size_bytes: number;
  created_utc: string;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} Б`;
  const units = ["КБ", "МБ", "ГБ", "ТБ"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ru-RU");
  } catch {
    return iso;
  }
}

function Banner({
  kind,
  children,
}: {
  kind: "ok" | "err";
  children: React.ReactNode;
}) {
  const cls =
    kind === "ok"
      ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
      : "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
  return (
    <div className={`rounded-md px-3 py-2 text-sm ${cls}`}>{children}</div>
  );
}

function BackupsContent() {
  const [backups, setBackups] = useState<BackupInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`${BASE}/backups`, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setBackups(await r.json());
    } catch (e) {
      setMsg({ kind: "err", text: `Не удалось загрузить список: ${e}` });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const createBackup = async () => {
    setBusy("create");
    setMsg(null);
    try {
      const r = await fetch(`${BASE}/backup`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const res = await r.json();
      setMsg({
        kind: "ok",
        text: `Бэкап создан: ${res.name} (${formatBytes(res.size_bytes)}, компоненты: ${res.components.join(", ")})`,
      });
      await refresh();
    } catch (e) {
      setMsg({ kind: "err", text: `Ошибка создания бэкапа: ${e}` });
    } finally {
      setBusy(null);
    }
  };

  const downloadBackup = (name: string) => {
    // Stream via the authenticated session — open in a new tab so the browser
    // handles the file download with cookies attached.
    window.open(
      `${BASE}/backups/${encodeURIComponent(name)}/download`,
      "_blank",
    );
  };

  const restoreBackup = async (name: string) => {
    if (
      !window.confirm(
        `ВОССТАНОВЛЕНИЕ из «${name}».\n\nЭто ПЕРЕЗАПИШЕТ текущие данные (БД, MinIO, Qdrant, Redis). ` +
          `Сервисы хранилищ будут кратко перезапущены. Рекомендуется перезапустить backend после.\n\nПродолжить?`,
      )
    )
      return;
    setBusy(`restore:${name}`);
    setMsg(null);
    try {
      const r = await fetch(
        `${BASE}/backups/${encodeURIComponent(name)}/restore`,
        { method: "POST", credentials: "include", headers: csrfHeaders() },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const res = await r.json();
      setMsg({
        kind: "ok",
        text: `Восстановлено: ${res.restored.join(", ") || "—"}${res.skipped.length ? `; пропущено: ${res.skipped.join(", ")}` : ""}. ${res.note}`,
      });
    } catch (e) {
      setMsg({ kind: "err", text: `Ошибка восстановления: ${e}` });
    } finally {
      setBusy(null);
    }
  };

  const deleteBackup = async (name: string) => {
    if (!window.confirm(`Удалить архив «${name}»?`)) return;
    setBusy(`delete:${name}`);
    try {
      const r = await fetch(`${BASE}/backups/${encodeURIComponent(name)}`, {
        method: "DELETE",
        credentials: "include",
        headers: csrfHeaders(),
      });
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      setMsg({ kind: "ok", text: `Удалён: ${name}` });
      await refresh();
    } catch (e) {
      setMsg({ kind: "err", text: `Ошибка удаления: ${e}` });
    } finally {
      setBusy(null);
    }
  };

  const uploadBackup = async (file: File) => {
    setBusy("upload");
    setMsg(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch(`${BASE}/backups/upload`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(), // no Content-Type — browser sets multipart boundary
        body: fd,
      });
      if (!r.ok) {
        const detail = await r.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${r.status}`);
      }
      setMsg({ kind: "ok", text: `Загружен: ${file.name}` });
      await refresh();
    } catch (e) {
      setMsg({ kind: "err", text: `Ошибка загрузки: ${e}` });
    } finally {
      setBusy(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={createBackup}
          disabled={busy === "create"}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {busy === "create" ? "Создаю…" : "Создать бэкап"}
        </button>

        <button
          onClick={() => fileRef.current?.click()}
          disabled={busy === "upload"}
          className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
        >
          {busy === "upload" ? "Загружаю…" : "Загрузить архив"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".tar.gz,application/gzip"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void uploadBackup(f);
          }}
        />

        <button
          onClick={refresh}
          className="ml-auto rounded-md border border-border px-3 py-2 text-sm hover:bg-accent"
        >
          Обновить
        </button>
      </div>

      <p className="text-sm text-muted-foreground">
        Бэкап включает PostgreSQL, MinIO, Qdrant и Redis. Архивы хранятся на
        сервере; их можно скачать, восстановить или загрузить с другого сервера
        (перенос базы).
      </p>

      {msg && <Banner kind={msg.kind}>{msg.text}</Banner>}

      <div className="rounded-lg border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-4 py-2 text-left font-medium">Архив</th>
              <th className="px-4 py-2 text-right font-medium">Размер</th>
              <th className="px-4 py-2 text-left font-medium">Создан</th>
              <th className="px-4 py-2 text-right font-medium">Действия</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-6 text-center text-muted-foreground"
                >
                  Загрузка…
                </td>
              </tr>
            ) : backups.length === 0 ? (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-6 text-center text-muted-foreground"
                >
                  Бэкапов пока нет. Нажмите «Создать бэкап».
                </td>
              </tr>
            ) : (
              backups.map((b) => (
                <tr key={b.name} className="border-t border-border">
                  <td className="px-4 py-2 font-mono text-xs">{b.name}</td>
                  <td className="px-4 py-2 text-right">
                    {formatBytes(b.size_bytes)}
                  </td>
                  <td className="px-4 py-2">{formatDate(b.created_utc)}</td>
                  <td className="px-4 py-2">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => downloadBackup(b.name)}
                        className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
                      >
                        Скачать
                      </button>
                      <button
                        onClick={() => restoreBackup(b.name)}
                        disabled={busy === `restore:${b.name}`}
                        className="rounded border border-amber-500 px-2 py-1 text-xs text-amber-700 dark:text-amber-300 hover:bg-amber-50 dark:hover:bg-amber-950 disabled:opacity-50"
                      >
                        {busy === `restore:${b.name}`
                          ? "Восстанавливаю…"
                          : "Восстановить"}
                      </button>
                      <button
                        onClick={() => deleteBackup(b.name)}
                        disabled={busy === `delete:${b.name}`}
                        className="rounded border border-destructive px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                      >
                        Удалить
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function BackupsPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <BackupsContent />
    </ProtectedRoute>
  );
}
