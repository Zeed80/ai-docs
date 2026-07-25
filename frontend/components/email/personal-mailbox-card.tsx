"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();

interface UserMailboxOut {
  address: string | null;
  is_active: boolean | null;
  webmail_url: string | null;
  last_sync_at: string | null;
  sync_error: string | null;
  sweep_enabled: boolean | null;
  quota_mb: number | null;
}

export function PersonalMailboxCard() {
  const [mailbox, setMailbox] = useState<UserMailboxOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/mailbox/me`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: UserMailboxOut | null) => setMailbox(d))
      .catch(() => setMailbox(null))
      .finally(() => setLoading(false));
  }, []);

  async function toggleSweep(enabled: boolean) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/mailbox/me/sweep`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ sweep_enabled: enabled }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setMailbox(await res.json());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-slate-700 bg-slate-800 p-5 space-y-3">
      <h3 className="text-sm font-medium text-slate-200">
        Моя корпоративная почта
      </h3>

      {loading ? (
        <p className="text-xs text-slate-400">Загрузка...</p>
      ) : mailbox?.address ? (
        <div className="space-y-3">
          <p className="text-sm text-slate-200">
            <span className="font-mono">{mailbox.address}</span>{" "}
            {mailbox.is_active === false && (
              <span className="text-amber-400 text-xs">(отключён)</span>
            )}
          </p>
          <div className="flex flex-wrap gap-3 text-xs">
            {mailbox.webmail_url && (
              <a
                href={mailbox.webmail_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-400 hover:underline"
              >
                Открыть вебмейл ↗
              </a>
            )}
            <a
              href={`/email?mailbox=${encodeURIComponent(mailbox.address)}`}
              className="text-blue-400 hover:underline"
            >
              Мои письма в приложении →
            </a>
          </div>

          {/* Согласие, отзываемое в любой момент. Формулировка важна: человек
              должен понимать, что именно получает агент, включая галочку. */}
          <label className="flex items-start gap-2 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={!!mailbox.sweep_enabled}
              disabled={busy}
              onChange={(e) => toggleSweep(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              Разрешить ИИ-сотруднику читать этот ящик
              <span className="block text-slate-400">
                Новые письма и вложения будут забираться в систему и
                обрабатываться (распознавание счетов и документов). Переписка
                остаётся видимой только вам, отметки «прочитано» в вашем
                почтовом клиенте не затрагиваются. Выключить можно в любой
                момент.
              </span>
            </span>
          </label>

          {mailbox.quota_mb ? (
            <p className="text-xs text-slate-400">
              Квота ящика: {mailbox.quota_mb} МБ
            </p>
          ) : null}

          {mailbox.sync_error && (
            <p className="text-xs text-red-400">
              Ошибка синхронизации: {mailbox.sync_error}
            </p>
          )}
          {error && <p className="text-xs text-red-400">{error}</p>}
        </div>
      ) : (
        <p className="text-xs text-slate-400">
          Адрес не назначен — обратитесь к администратору, чтобы получить личный
          почтовый ящик.
        </p>
      )}
    </div>
  );
}
