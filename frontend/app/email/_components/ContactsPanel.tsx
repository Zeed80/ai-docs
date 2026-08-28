"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { emailApi } from "./api";

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

function initials(s: string) {
  const p = s.replace(/[<>]/g, "").trim().split(/[\s@.]+/).filter(Boolean);
  return ((p[0]?.[0] ?? "?") + (p[1]?.[0] ?? "")).toUpperCase();
}
function colorFor(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return `hsl(${h} 45% 40%)`;
}

const blank = (): Partial<Contact> => ({ tags: [] });

export function ContactsPanel({ onCompose }: { onCompose: (email: string) => void }) {
  const [rows, setRows] = useState<Contact[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [favOnly, setFavOnly] = useState(false);
  const [tag, setTag] = useState<string | null>(null);
  const [sel, setSel] = useState<Contact | null>(null);
  const [edit, setEdit] = useState<Partial<Contact> | null>(null);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    emailApi
      .contactBook({ q: q.trim() || undefined, favorites: favOnly, tag: tag || undefined })
      .then(setRows)
      .catch(() => setRows([]));
    emailApi.contactTags().then(setTags).catch(() => {});
  }, [q, favOnly, tag]);

  useEffect(() => {
    const h = setTimeout(load, 200);
    return () => clearTimeout(h);
  }, [load]);

  async function save() {
    if (!edit?.email) return;
    const body = {
      email: edit.email,
      name: edit.name || null,
      organization: edit.organization || null,
      phone: edit.phone || null,
      notes: edit.notes || null,
      is_favorite: !!edit.is_favorite,
      tags: edit.tags ?? [],
    };
    if (edit.id) await emailApi.updateContact(edit.id, body);
    else await emailApi.createContact(body);
    setEdit(null);
    load();
  }

  async function toggleFav(c: Contact) {
    await emailApi.updateContact(c.id, { is_favorite: !c.is_favorite });
    load();
    if (sel?.id === c.id) setSel({ ...sel, is_favorite: !c.is_favorite });
  }

  async function onFile(f: File | null) {
    if (!f) return;
    setImporting(true);
    try {
      const csv = await f.text();
      const res = await emailApi.importContacts(csv);
      alert(`Импорт: +${res.added}, обновлено ${res.updated}, пропущено ${res.skipped}`);
      load();
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  const inp = "w-full rounded border border-slate-600 bg-slate-700 px-2 py-1.5 text-sm text-slate-100 placeholder-slate-500";

  return (
    <div className="flex h-full">
      {/* list */}
      <div className="flex w-80 shrink-0 flex-col border-r border-slate-700 bg-slate-800/40">
        <div className="space-y-1.5 border-b border-slate-700 p-2">
          <div className="flex items-center gap-1.5">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Поиск контактов"
              className="flex-1 rounded bg-slate-700 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <button
              onClick={() => setEdit(blank())}
              className="rounded bg-blue-600 px-2.5 py-1.5 text-xs text-white hover:bg-blue-500"
            >
              + Контакт
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-1 text-xs">
            <button
              onClick={() => setFavOnly((v) => !v)}
              className={`rounded-full px-2 py-0.5 ${favOnly ? "bg-amber-600 text-white" : "text-slate-400 hover:bg-slate-700"}`}
            >
              ★ Избранные
            </button>
            {tags.map((tg) => (
              <button
                key={tg}
                onClick={() => setTag(tag === tg ? null : tg)}
                className={`rounded-full px-2 py-0.5 ${tag === tg ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-700"}`}
              >
                {tg}
              </button>
            ))}
            <span className="ml-auto flex gap-1">
              <a
                href={emailApi.exportContactsUrl()}
                className="text-slate-500 hover:text-slate-300"
                title="Экспорт CSV"
              >
                ↓CSV
              </a>
              <button
                onClick={() => fileRef.current?.click()}
                disabled={importing}
                className="text-slate-500 hover:text-slate-300 disabled:opacity-50"
                title="Импорт CSV"
              >
                ↑CSV
              </button>
              <input
                ref={fileRef}
                type="file"
                accept=".csv,text/csv"
                hidden
                onChange={(e) => onFile(e.target.files?.[0] ?? null)}
              />
            </span>
          </div>
        </div>
        <div className="flex-1 overflow-auto">
          {rows.length === 0 && (
            <p className="py-8 text-center text-xs text-slate-500">Контактов нет</p>
          )}
          {rows.map((c) => (
            <button
              key={c.id}
              onClick={() => setSel(c)}
              className={`flex w-full items-center gap-2 border-b border-slate-800 px-3 py-2 text-left ${
                sel?.id === c.id ? "bg-blue-900/30" : "hover:bg-slate-800/60"
              }`}
            >
              <span
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white"
                style={{ background: colorFor(c.email) }}
              >
                {initials(c.name || c.email)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1">
                  <span className="truncate text-sm text-slate-100">{c.name || c.email}</span>
                  {c.is_favorite && <span className="text-xs text-amber-400">★</span>}
                </span>
                <span className="block truncate text-xs text-slate-400">
                  {c.name ? c.email : c.organization || ""}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* detail */}
      <div className="flex-1 overflow-auto bg-slate-900 p-6">
        {sel ? (
          <div className="mx-auto max-w-xl">
            <div className="flex items-center gap-3">
              <span
                className="flex h-14 w-14 items-center justify-center rounded-full text-lg font-bold text-white"
                style={{ background: colorFor(sel.email) }}
              >
                {initials(sel.name || sel.email)}
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="truncate text-lg font-semibold text-slate-100">
                  {sel.name || sel.email}
                </h2>
                {sel.organization && (
                  <p className="text-sm text-slate-400">{sel.organization}</p>
                )}
              </div>
              <button onClick={() => toggleFav(sel)} className="text-xl">
                <span className={sel.is_favorite ? "text-amber-400" : "text-slate-600"}>★</span>
              </button>
            </div>

            <div className="mt-4 space-y-2 text-sm">
              <Row label="Email">
                <button onClick={() => onCompose(sel.email)} className="text-blue-400 hover:underline">
                  {sel.email}
                </button>
              </Row>
              {sel.phone && <Row label="Телефон">{sel.phone}</Row>}
              {sel.tags.length > 0 && (
                <Row label="Метки">
                  <span className="flex flex-wrap gap-1">
                    {sel.tags.map((t) => (
                      <span key={t} className="rounded-full bg-slate-700 px-2 text-xs text-slate-300">
                        {t}
                      </span>
                    ))}
                  </span>
                </Row>
              )}
              {sel.notes && <Row label="Заметки">{sel.notes}</Row>}
              <Row label="">
                <span className="text-xs text-slate-500">
                  использован {sel.use_count}× ·{" "}
                  {sel.source === "auto" ? "добавлен автоматически" : "добавлен вручную"}
                </span>
              </Row>
            </div>

            <div className="mt-5 flex gap-2">
              <button
                onClick={() => onCompose(sel.email)}
                className="rounded bg-blue-600 px-4 py-1.5 text-sm text-white hover:bg-blue-500"
              >
                Написать письмо
              </button>
              <button
                onClick={() => setEdit(sel)}
                className="rounded border border-slate-600 px-4 py-1.5 text-sm text-slate-300 hover:bg-slate-700"
              >
                Изменить
              </button>
              <button
                onClick={async () => {
                  if (confirm("Удалить контакт?")) {
                    await emailApi.deleteContact(sel.id);
                    setSel(null);
                    load();
                  }
                }}
                className="rounded border border-slate-600 px-4 py-1.5 text-sm text-slate-400 hover:bg-red-900/40 hover:text-red-300"
              >
                Удалить
              </button>
            </div>
          </div>
        ) : (
          <div className="flex h-full items-center justify-center text-center text-slate-500">
            <div>
              <div className="mb-2 text-4xl">👤</div>
              <p className="text-sm">Выберите контакт</p>
            </div>
          </div>
        )}
      </div>

      {edit && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md space-y-2 rounded-xl border border-slate-700 bg-slate-800 p-4">
            <h3 className="text-sm font-semibold text-slate-100">
              {edit.id ? "Изменить контакт" : "Новый контакт"}
            </h3>
            <input
              value={edit.email ?? ""}
              disabled={!!edit.id}
              onChange={(e) => setEdit({ ...edit, email: e.target.value })}
              placeholder="email@example.com"
              className={inp}
            />
            <input
              value={edit.name ?? ""}
              onChange={(e) => setEdit({ ...edit, name: e.target.value })}
              placeholder="Имя"
              className={inp}
            />
            <input
              value={edit.organization ?? ""}
              onChange={(e) => setEdit({ ...edit, organization: e.target.value })}
              placeholder="Организация"
              className={inp}
            />
            <input
              value={edit.phone ?? ""}
              onChange={(e) => setEdit({ ...edit, phone: e.target.value })}
              placeholder="Телефон"
              className={inp}
            />
            <input
              value={(edit.tags ?? []).join(", ")}
              onChange={(e) =>
                setEdit({
                  ...edit,
                  tags: e.target.value.split(",").map((x) => x.trim()).filter(Boolean),
                })
              }
              placeholder="Метки через запятую"
              className={inp}
            />
            <textarea
              value={edit.notes ?? ""}
              onChange={(e) => setEdit({ ...edit, notes: e.target.value })}
              placeholder="Заметки"
              rows={2}
              className={`${inp} resize-none`}
            />
            <label className="flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={!!edit.is_favorite}
                onChange={(e) => setEdit({ ...edit, is_favorite: e.target.checked })}
              />
              избранный
            </label>
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
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3">
      <span className="w-20 shrink-0 text-xs text-slate-500">{label}</span>
      <span className="min-w-0 flex-1 text-slate-200">{children}</span>
    </div>
  );
}
