"use client";

/**
 * Секреты браузера (E34, T3): пароли, которые сотрудник вводит на сайтах через
 * брокер `computer_use.desktop_fill_secret`, не видя их значения.
 *
 * Создаёт и отзывает их только человек; список никогда не показывает значение.
 * Пароль привязан к сайту (origin) и к гранту: агент может ввести его только
 * на этом сайте и только по гранту, где он перечислен.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();
const URL = `${API}/api/browser-secrets`;

type Secret = {
  id: string;
  label: string;
  origin: string;
  created_at: string;
  revoked_at: string | null;
};

const field =
  "min-w-0 rounded-md border border-border bg-background px-3 py-2 text-sm " +
  "focus:outline-none focus:ring-2 focus:ring-primary";

function detail(body: unknown, status: number): string {
  const d = (body as { detail?: unknown })?.detail;
  return typeof d === "string" ? d : `Ошибка ${status}`;
}

function BrowserSecretsContent() {
  const [secrets, setSecrets] = useState<Secret[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [origin, setOrigin] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(URL, { credentials: "include", cache: "no-store" });
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        setError(detail(body, r.status));
        return;
      }
      setSecrets(body as Secret[]);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(URL, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          label: label.trim(),
          origin: origin.trim(),
          value,
        }),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        setError(detail(body, r.status));
        return;
      }
      setLabel("");
      setOrigin("");
      setValue("");
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(secret: Secret) {
    if (
      !window.confirm(
        `Отозвать «${secret.label}» (${secret.origin})?\n\n` +
          "Агент больше не сможет ввести этот пароль, в том числе по уже выданным грантам. " +
          "Вернуть отозванный пароль нельзя — только добавить заново.",
      )
    )
      return;
    setError(null);
    try {
      const r = await fetch(`${URL}/${secret.id}/revoke`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        setError(detail(body, r.status));
        return;
      }
      await load();
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Пароли для браузера</h2>
        <p className="mt-1 break-words text-sm text-muted-foreground">
          Сотрудник вводит эти пароли на сайтах, не видя их: значение хранится
          зашифрованным и не возвращается ни здесь, ни агенту. Пароль вводится
          только на своём сайте и только по гранту, где он перечислен. Добавить
          и отозвать пароль может только человек.
        </p>
      </div>

      {error && (
        <p role="alert" className="break-words text-sm text-red-500">
          {error}
        </p>
      )}

      <form
        onSubmit={add}
        className="flex flex-wrap items-end gap-2"
        aria-label="Новый пароль"
      >
        <input
          className={`${field} flex-1 basis-40`}
          placeholder="Название (Кабинет поставщика)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          required
          aria-label="Название"
        />
        <input
          className={`${field} flex-1 basis-48`}
          placeholder="https://portal.example.ru"
          value={origin}
          onChange={(e) => setOrigin(e.target.value)}
          required
          inputMode="url"
          aria-label="Сайт"
        />
        <input
          className={`${field} flex-1 basis-40`}
          type="password"
          autoComplete="new-password"
          placeholder="Пароль"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          required
          aria-label="Пароль"
        />
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          Сохранить
        </button>
      </form>

      {secrets === null ? null : secrets.length === 0 ? (
        <p className="text-sm text-muted-foreground">Паролей пока нет.</p>
      ) : (
        <ul
          className="divide-y divide-border rounded-md border border-border"
          aria-label="Пароли"
        >
          {secrets.map((s) => (
            <li
              key={s.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <div className="break-words text-sm font-medium">{s.label}</div>
                <div className="break-all text-xs text-muted-foreground">
                  {s.origin} · добавлен{" "}
                  {new Date(s.created_at).toLocaleString("ru-RU")}
                  {s.revoked_at &&
                    ` · отозван ${new Date(s.revoked_at).toLocaleString("ru-RU")}`}
                </div>
              </div>
              {s.revoked_at ? (
                <span className="text-xs text-muted-foreground">Отозван</span>
              ) : (
                <button
                  type="button"
                  onClick={() => void revoke(s)}
                  className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
                  aria-label={`Отозвать ${s.label}`}
                >
                  Отозвать
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function BrowserSecretsPage() {
  return (
    <ProtectedRoute>
      <BrowserSecretsContent />
    </ProtectedRoute>
  );
}
