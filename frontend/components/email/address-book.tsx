"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";
import { useHasRole } from "@/lib/rbac";

const API = getApiBaseUrl();

interface Contact {
  id: string;
  email: string;
  name: string | null;
  organization: string | null;
  phone: string | null;
  notes: string | null;
  tags: string[];
  is_favorite: boolean;
  source: string;
  use_count: number;
}
interface Signature {
  id: string;
  name: string;
  body_html: string;
  mailbox: string | null;
  owner_sub: string | null;
  is_default: boolean;
}

const card = "rounded-xl border border-slate-700 bg-slate-800 p-5";
const inp =
  "px-2 py-1 text-sm bg-slate-700 border border-slate-600 text-slate-200 rounded";

// ── Address book ────────────────────────────────────────────────────────────

export function AddressBookSection() {
  const [rows, setRows] = useState<Contact[]>([]);
  const [q, setQ] = useState("");
  const [edit, setEdit] = useState<Partial<Contact> | null>(null);
  const isAdmin = useHasRole("admin");

  const load = () =>
    apiFetch(`${API}/api/email/contacts/book?q=${encodeURIComponent(q)}`)
      .then((r) => r.json())
      .then((d) => setRows(Array.isArray(d) ? d : []))
      .catch(() => setRows([]));
  useEffect(() => {
    const h = setTimeout(load, 250);
    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  async function save() {
    if (!edit?.email) return;
    const body = {
      email: edit.email,
      name: edit.name,
      organization: edit.organization,
      phone: edit.phone,
      notes: edit.notes,
      is_favorite: !!edit.is_favorite,
      tags: edit.tags ?? [],
    };
    const res = edit.id
      ? await mutFetch(`${API}/api/email/contacts/book/${edit.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
      : await mutFetch(`${API}/api/email/contacts/book`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    if (res.ok) {
      setEdit(null);
      load();
    }
  }

  return (
    <section className={card}>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-100">Адресная книга</h3>
          <p className="text-xs text-slate-400">
            Контакты для автодополнения в письмах. {isAdmin && "Общие видны всем."}
          </p>
        </div>
        <button
          onClick={() => setEdit({})}
          className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500"
        >
          + Контакт
        </button>
      </div>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Поиск по имени / адресу / организации"
        className={`${inp} mb-3 w-full`}
      />
      <div className="space-y-1.5">
        {rows.length === 0 && <p className="text-xs text-slate-500">Пусто</p>}
        {rows.map((c) => (
          <div
            key={c.id}
            className="flex items-center gap-2 rounded border border-slate-700 bg-slate-900/40 px-3 py-1.5 text-xs"
          >
            <button
              onClick={() =>
                mutFetch(`${API}/api/email/contacts/book/${c.id}`, {
                  method: "PATCH",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ is_favorite: !c.is_favorite }),
                }).then(load)
              }
              className={c.is_favorite ? "text-amber-400" : "text-slate-600 hover:text-slate-400"}
            >
              {c.is_favorite ? "★" : "☆"}
            </button>
            <span className="font-medium text-slate-200">{c.name || c.email}</span>
            {c.name && <span className="text-slate-500">{c.email}</span>}
            {c.organization && <span className="text-slate-500">· {c.organization}</span>}
            {c.source === "auto" && (
              <span className="rounded bg-slate-700 px-1 text-[10px] text-slate-400">авто</span>
            )}
            <span className="ml-auto flex gap-1.5">
              <button onClick={() => setEdit(c)} className="text-slate-400 hover:text-slate-200">
                Изм.
              </button>
              <button
                onClick={() =>
                  mutFetch(`${API}/api/email/contacts/book/${c.id}`, { method: "DELETE" }).then(load)
                }
                className="text-slate-500 hover:text-red-400"
              >
                ✕
              </button>
            </span>
          </div>
        ))}
      </div>

      {edit && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md space-y-2 rounded-xl border border-slate-700 bg-slate-800 p-4">
            <input
              value={edit.email ?? ""}
              disabled={!!edit.id}
              onChange={(e) => setEdit({ ...edit, email: e.target.value })}
              placeholder="email@example.com"
              className={`${inp} w-full`}
            />
            <input
              value={edit.name ?? ""}
              onChange={(e) => setEdit({ ...edit, name: e.target.value })}
              placeholder="Имя"
              className={`${inp} w-full`}
            />
            <input
              value={edit.organization ?? ""}
              onChange={(e) => setEdit({ ...edit, organization: e.target.value })}
              placeholder="Организация"
              className={`${inp} w-full`}
            />
            <input
              value={edit.phone ?? ""}
              onChange={(e) => setEdit({ ...edit, phone: e.target.value })}
              placeholder="Телефон"
              className={`${inp} w-full`}
            />
            <textarea
              value={edit.notes ?? ""}
              onChange={(e) => setEdit({ ...edit, notes: e.target.value })}
              placeholder="Заметки"
              rows={2}
              className={`${inp} w-full resize-none`}
            />
            <div className="flex gap-2 pt-1">
              <button
                onClick={save}
                className="rounded bg-blue-600 px-4 py-1.5 text-xs text-white hover:bg-blue-500"
              >
                Сохранить
              </button>
              <button
                onClick={() => setEdit(null)}
                className="rounded border border-slate-600 px-4 py-1.5 text-xs text-slate-300 hover:bg-slate-700"
              >
                Отмена
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// ── Signatures ──────────────────────────────────────────────────────────────

export function SignaturesSection() {
  const [rows, setRows] = useState<Signature[]>([]);
  const [mailboxes, setMailboxes] = useState<string[]>([]);
  const [edit, setEdit] = useState<Partial<Signature> | null>(null);
  const isAdmin = useHasRole("admin");

  const load = () =>
    apiFetch(`${API}/api/email/signatures`)
      .then((r) => r.json())
      .then((d) => setRows(Array.isArray(d) ? d : []))
      .catch(() => setRows([]));
  useEffect(() => {
    load();
    apiFetch(`${API}/api/email/mailboxes`)
      .then((r) => r.json())
      .then((d) => setMailboxes(Array.isArray(d) ? d.map((m: { name: string }) => m.name) : []))
      .catch(() => {});
  }, []);

  async function save() {
    if (!edit?.name || !edit.body_html) return;
    const body = {
      name: edit.name,
      body_html: edit.body_html,
      mailbox: edit.mailbox || null,
      is_default: !!edit.is_default,
      shared: !!edit.mailbox,
    };
    const res = edit.id
      ? await mutFetch(`${API}/api/email/signatures/${edit.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
      : await mutFetch(`${API}/api/email/signatures`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    if (res.ok) {
      setEdit(null);
      load();
    }
  }

  return (
    <section className={card}>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-100">Подписи</h3>
          <p className="text-xs text-slate-400">
            Подставляются в конец письма. Приоритет: подпись ящика → личная.
          </p>
        </div>
        <button
          onClick={() => setEdit({ body_html: "<p>С уважением,<br/>" })}
          className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500"
        >
          + Подпись
        </button>
      </div>
      <div className="space-y-1.5">
        {rows.length === 0 && <p className="text-xs text-slate-500">Подписей нет</p>}
        {rows.map((sg) => (
          <div
            key={sg.id}
            className="flex items-center gap-2 rounded border border-slate-700 bg-slate-900/40 px-3 py-1.5 text-xs"
          >
            <span className="font-medium text-slate-200">{sg.name}</span>
            {sg.mailbox && (
              <span className="rounded bg-slate-700 px-1 text-[10px] text-slate-400">
                ящик: {sg.mailbox}
              </span>
            )}
            {sg.owner_sub && (
              <span className="rounded bg-slate-700 px-1 text-[10px] text-slate-400">личная</span>
            )}
            {sg.is_default && <span className="text-[10px] text-emerald-400">по умолчанию</span>}
            <span className="ml-auto flex gap-1.5">
              <button onClick={() => setEdit(sg)} className="text-slate-400 hover:text-slate-200">
                Изм.
              </button>
              <button
                onClick={() =>
                  mutFetch(`${API}/api/email/signatures/${sg.id}`, { method: "DELETE" }).then(load)
                }
                className="text-slate-500 hover:text-red-400"
              >
                ✕
              </button>
            </span>
          </div>
        ))}
      </div>

      {edit && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-lg space-y-2 rounded-xl border border-slate-700 bg-slate-800 p-4">
            <input
              value={edit.name ?? ""}
              onChange={(e) => setEdit({ ...edit, name: e.target.value })}
              placeholder="Название подписи"
              className={`${inp} w-full`}
            />
            {isAdmin && mailboxes.length > 0 && (
              <select
                value={edit.mailbox ?? ""}
                onChange={(e) => setEdit({ ...edit, mailbox: e.target.value })}
                className={`${inp} w-full`}
              >
                <option value="">Личная подпись</option>
                {mailboxes.map((m) => (
                  <option key={m} value={m}>
                    Подпись ящика «{m}»
                  </option>
                ))}
              </select>
            )}
            <textarea
              value={edit.body_html ?? ""}
              onChange={(e) => setEdit({ ...edit, body_html: e.target.value })}
              placeholder="<p>С уважением,<br/>Иван Петров</p>"
              rows={5}
              className={`${inp} w-full resize-none font-mono text-xs`}
            />
            <label className="flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={!!edit.is_default}
                onChange={(e) => setEdit({ ...edit, is_default: e.target.checked })}
              />
              подпись по умолчанию
            </label>
            <div className="rounded border border-slate-700 bg-slate-900 p-2 text-xs text-slate-300">
              <p className="mb-1 text-[10px] text-slate-500">Предпросмотр:</p>
              <div dangerouslySetInnerHTML={{ __html: edit.body_html ?? "" }} />
            </div>
            <div className="flex gap-2 pt-1">
              <button
                onClick={save}
                className="rounded bg-blue-600 px-4 py-1.5 text-xs text-white hover:bg-blue-500"
              >
                Сохранить
              </button>
              <button
                onClick={() => setEdit(null)}
                className="rounded border border-slate-600 px-4 py-1.5 text-xs text-slate-300 hover:bg-slate-700"
              >
                Отмена
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// ── Email policy (admin) ────────────────────────────────────────────────────

export function EmailPolicySection() {
  const [p, setP] = useState<{
    auto_send_enabled: boolean;
    auto_send_max_per_day: number;
    attachment_retention_days: number;
  } | null>(null);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    apiFetch(`${API}/api/email/policy`)
      .then((r) => r.json())
      .then(setP)
      .catch(() => {});
  }, []);

  async function put(patch: Record<string, unknown>) {
    const res = await mutFetch(`${API}/api/email/policy`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (res.ok) setP(await res.json());
  }

  if (!p) return null;

  return (
    <section className={card}>
      <h3 className="text-sm font-semibold text-slate-100">Политика почты</h3>
      <p className="mb-3 text-xs text-slate-400">Защищённые настройки. Только администратор.</p>

      <div className="flex items-center justify-between border-b border-slate-700 py-2 text-sm">
        <div>
          <span className="text-slate-200">Авто-отправка ответов по правилам</span>
          <p className="text-xs text-slate-500">
            Разрешить фильтрам отправлять шаблонные ответы без человека (с ограничениями).
          </p>
        </div>
        {p.auto_send_enabled ? (
          <button
            onClick={() => put({ auto_send_enabled: false })}
            className="rounded bg-emerald-700 px-3 py-1 text-xs text-white hover:bg-emerald-600"
          >
            Включено — выключить
          </button>
        ) : confirming ? (
          <span className="flex gap-1">
            <button
              onClick={() => {
                put({ auto_send_enabled: true });
                setConfirming(false);
              }}
              className="rounded bg-red-600 px-2 py-1 text-xs text-white"
            >
              Да, включить
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300"
            >
              Нет
            </button>
          </span>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            className="rounded border border-slate-600 px-3 py-1 text-xs text-slate-300 hover:bg-slate-700"
          >
            Выключено — включить
          </button>
        )}
      </div>

      <div className="flex items-center justify-between border-b border-slate-700 py-2 text-sm">
        <span className="text-slate-200">Лимит авто-ответов в сутки</span>
        <input
          type="number"
          value={p.auto_send_max_per_day}
          onChange={(e) => put({ auto_send_max_per_day: Number(e.target.value) })}
          className={`${inp} w-20`}
        />
      </div>

      <div className="flex items-center justify-between py-2 text-sm">
        <div>
          <span className="text-slate-200">Хранить вложения писем, дней</span>
          <p className="text-xs text-slate-500">
            Байты старых вложений удаляются; строка вложения (имя/размер) остаётся.
          </p>
        </div>
        <input
          type="number"
          value={p.attachment_retention_days}
          onChange={(e) => put({ attachment_retention_days: Number(e.target.value) })}
          className={`${inp} w-20`}
        />
      </div>
    </section>
  );
}
