"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

interface UserMailboxOut {
  address: string | null;
  is_active: boolean | null;
  webmail_url: string | null;
  last_sync_at: string | null;
  sync_error: string | null;
}

export function PersonalMailboxCard() {
  const [mailbox, setMailbox] = useState<UserMailboxOut | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/api/mailbox/me`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: UserMailboxOut | null) => setMailbox(d))
      .catch(() => setMailbox(null))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="rounded-xl border border-slate-700 bg-slate-800 p-5 space-y-3">
      <h3 className="text-sm font-medium text-slate-200">
        Моя корпоративная почта
      </h3>

      {loading ? (
        <p className="text-xs text-slate-400">Загрузка...</p>
      ) : mailbox?.address ? (
        <div className="space-y-2">
          <p className="text-sm text-slate-200">
            <span className="font-mono">{mailbox.address}</span>{" "}
            {mailbox.is_active === false && (
              <span className="text-amber-400 text-xs">(отозван)</span>
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
          {mailbox.sync_error && (
            <p className="text-xs text-red-400">
              Ошибка синхронизации: {mailbox.sync_error}
            </p>
          )}
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
