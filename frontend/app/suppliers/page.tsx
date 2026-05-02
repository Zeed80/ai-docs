"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

const API = getApiBaseUrl();

interface Supplier {
  id: string;
  name: string;
  inn: string | null;
  role: string;
  contact_email: string | null;
  contact_phone: string | null;
  user_rating: number | null;
  user_notes: string | null;
}

interface DuplicateMatch {
  id: string;
  name: string;
  inn: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  match_reason: string;
}

const EMPTY_FORM = {
  name: "",
  inn: "",
  kpp: "",
  ogrn: "",
  address: "",
  contact_email: "",
  contact_phone: "",
  bank_name: "",
  bank_bik: "",
  bank_account: "",
  corr_account: "",
  user_notes: "",
  user_rating: 0,
};

function StarRating({
  value,
  onChange,
  readonly,
}: {
  value: number;
  onChange?: (v: number) => void;
  readonly?: boolean;
}) {
  const [hover, setHover] = useState(0);
  return (
    <span className="flex gap-0.5">
      {[1, 2, 3, 4, 5].map((star) => (
        <button
          key={star}
          type="button"
          disabled={readonly}
          onClick={() => onChange?.(star === value ? 0 : star)}
          onMouseEnter={() => !readonly && setHover(star)}
          onMouseLeave={() => !readonly && setHover(0)}
          className={`text-base leading-none transition-colors ${readonly ? "cursor-default" : "cursor-pointer"} ${
            star <= (hover || value) ? "text-yellow-400" : "text-slate-600"
          }`}
        >
          ★
        </button>
      ))}
    </span>
  );
}

export default function SuppliersPage() {
  const router = useRouter();
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [creating, setCreating] = useState(false);
  const [duplicates, setDuplicates] = useState<DuplicateMatch[]>([]);
  const [dupChecked, setDupChecked] = useState(false);
  const [createError, setCreateError] = useState("");
  const [forceCreate, setForceCreate] = useState(false);
  const checkTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadSuppliers = useCallback(async (q: string) => {
    setLoading(true);
    try {
      if (q.length >= 2) {
        const resp = await fetch(`${API}/api/suppliers/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: q }),
        });
        const data = await resp.json();
        setSuppliers(data.results ?? []);
      } else {
        const resp = await fetch(`${API}/api/suppliers?role=supplier`);
        setSuppliers(await resp.json());
      }
    } catch {
      setSuppliers([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => loadSuppliers(search), 300);
    return () => clearTimeout(t);
  }, [search, loadSuppliers]);

  // Live duplicate check while filling the form
  useEffect(() => {
    if (!showCreate) return;
    if (!form.name.trim()) {
      setDuplicates([]);
      setDupChecked(false);
      return;
    }
    if (checkTimer.current) clearTimeout(checkTimer.current);
    checkTimer.current = setTimeout(async () => {
      try {
        const resp = await fetch(`${API}/api/suppliers/check-duplicate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: form.name,
            inn: form.inn || null,
            contact_email: form.contact_email || null,
            contact_phone: form.contact_phone || null,
            force: false,
          }),
        });
        const data = await resp.json();
        setDuplicates(data.matches ?? []);
        setDupChecked(true);
      } catch {
        setDuplicates([]);
      }
    }, 600);
    return () => {
      if (checkTimer.current) clearTimeout(checkTimer.current);
    };
  }, [form.name, form.inn, form.contact_email, form.contact_phone, showCreate]);

  async function handleCreate() {
    setCreating(true);
    setCreateError("");
    try {
      const body = {
        name: form.name.trim(),
        inn: form.inn || null,
        kpp: form.kpp || null,
        ogrn: form.ogrn || null,
        address: form.address || null,
        contact_email: form.contact_email || null,
        contact_phone: form.contact_phone || null,
        bank_name: form.bank_name || null,
        bank_bik: form.bank_bik || null,
        bank_account: form.bank_account || null,
        corr_account: form.corr_account || null,
        user_notes: form.user_notes || null,
        user_rating: form.user_rating || null,
        force: forceCreate,
      };
      const resp = await fetch(`${API}/api/suppliers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 409) {
        const data = await resp.json();
        setDuplicates(data.detail?.matches ?? []);
        setCreateError(
          "Найдены похожие поставщики. Проверьте список ниже или нажмите «Создать всё равно».",
        );
        return;
      }
      if (!resp.ok) {
        setCreateError("Ошибка создания поставщика");
        return;
      }
      setShowCreate(false);
      setForm({ ...EMPTY_FORM });
      setDuplicates([]);
      setForceCreate(false);
      loadSuppliers(search);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold text-slate-100">Поставщики</h1>
        <button
          onClick={() => {
            setShowCreate(true);
            setForm({ ...EMPTY_FORM });
            setDuplicates([]);
            setDupChecked(false);
            setCreateError("");
            setForceCreate(false);
          }}
          className="px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors"
        >
          + Добавить
        </button>
      </div>

      <input
        type="text"
        placeholder="Поиск по имени, ИНН..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        className="w-full px-4 py-2 mb-4 bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded-lg outline-none focus:border-blue-400"
      />

      {loading ? (
        <div className="text-slate-400 py-8 text-center text-sm">
          Загрузка...
        </div>
      ) : suppliers.length === 0 ? (
        <div className="text-slate-400 py-8 text-center text-sm">
          Нет поставщиков
        </div>
      ) : (
        <div className="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-700/50 text-slate-400 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-2">Название</th>
                <th className="text-left px-4 py-2">ИНН</th>
                <th className="text-left px-4 py-2">Email</th>
                <th className="text-left px-4 py-2">Телефон</th>
                <th className="text-center px-4 py-2">Рейтинг</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700">
              {suppliers.map((s) => (
                <tr
                  key={s.id}
                  className="cursor-pointer hover:bg-slate-700/50 transition-colors"
                  onClick={() => router.push(`/suppliers/${s.id}`)}
                >
                  <td className="px-4 py-2.5 font-medium text-slate-100">
                    {s.name}
                  </td>
                  <td className="px-4 py-2.5 text-slate-400 font-mono text-xs">
                    {s.inn ?? "—"}
                  </td>
                  <td className="px-4 py-2.5 text-slate-400 text-xs">
                    {s.contact_email ?? "—"}
                  </td>
                  <td className="px-4 py-2.5 text-slate-400 text-xs">
                    {s.contact_phone ?? "—"}
                  </td>
                  <td className="px-4 py-2.5 text-center">
                    {s.user_rating ? (
                      <StarRating value={s.user_rating} readonly />
                    ) : (
                      <span className="text-slate-600 text-xs">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create modal */}
      {showCreate && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
          onClick={() => setShowCreate(false)}
        >
          <div
            className="bg-slate-800 border border-slate-600 rounded-lg w-full max-w-2xl max-h-[90vh] overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
              <h2 className="text-base font-semibold text-slate-100">
                Новый поставщик
              </h2>
              <button
                onClick={() => setShowCreate(false)}
                className="text-slate-500 hover:text-slate-300 text-lg leading-none"
              >
                ×
              </button>
            </div>

            <div className="px-6 py-4 space-y-4">
              {/* Duplicate warning */}
              {dupChecked && duplicates.length > 0 && (
                <div className="bg-yellow-500/10 border border-yellow-500/30 rounded p-3 text-xs text-yellow-300 space-y-1">
                  <p className="font-semibold">
                    Похожие поставщики уже существуют:
                  </p>
                  {duplicates.map((d) => (
                    <div key={d.id} className="flex items-center gap-2">
                      <span className="text-yellow-400">•</span>
                      <button
                        type="button"
                        onClick={() => {
                          setShowCreate(false);
                          router.push(`/suppliers/${d.id}`);
                        }}
                        className="hover:underline text-yellow-200"
                      >
                        {d.name}
                      </button>
                      <span className="text-slate-400">({d.match_reason})</span>
                    </div>
                  ))}
                </div>
              )}
              {dupChecked && duplicates.length === 0 && form.name.trim() && (
                <p className="text-xs text-green-400">Дубликатов не найдено</p>
              )}

              {/* Main fields */}
              <div className="grid grid-cols-2 gap-3">
                <FormField
                  label="Название *"
                  value={form.name}
                  onChange={(v) => setForm((f) => ({ ...f, name: v }))}
                  className="col-span-2"
                />
                <FormField
                  label="ИНН"
                  value={form.inn}
                  onChange={(v) => setForm((f) => ({ ...f, inn: v }))}
                />
                <FormField
                  label="КПП"
                  value={form.kpp}
                  onChange={(v) => setForm((f) => ({ ...f, kpp: v }))}
                />
                <FormField
                  label="ОГРН"
                  value={form.ogrn}
                  onChange={(v) => setForm((f) => ({ ...f, ogrn: v }))}
                />
                <FormField
                  label="Email"
                  value={form.contact_email}
                  onChange={(v) => setForm((f) => ({ ...f, contact_email: v }))}
                />
                <FormField
                  label="Телефон"
                  value={form.contact_phone}
                  onChange={(v) => setForm((f) => ({ ...f, contact_phone: v }))}
                />
                <FormField
                  label="Адрес"
                  value={form.address}
                  onChange={(v) => setForm((f) => ({ ...f, address: v }))}
                  className="col-span-2"
                />
              </div>

              <details className="group">
                <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-200 select-none">
                  Банковские реквизиты
                </summary>
                <div className="grid grid-cols-2 gap-3 mt-3">
                  <FormField
                    label="Банк"
                    value={form.bank_name}
                    onChange={(v) => setForm((f) => ({ ...f, bank_name: v }))}
                    className="col-span-2"
                  />
                  <FormField
                    label="БИК"
                    value={form.bank_bik}
                    onChange={(v) => setForm((f) => ({ ...f, bank_bik: v }))}
                  />
                  <FormField
                    label="Р/с"
                    value={form.bank_account}
                    onChange={(v) =>
                      setForm((f) => ({ ...f, bank_account: v }))
                    }
                  />
                  <FormField
                    label="Корр. счёт"
                    value={form.corr_account}
                    onChange={(v) =>
                      setForm((f) => ({ ...f, corr_account: v }))
                    }
                  />
                </div>
              </details>

              <div>
                <label className="block text-xs text-slate-400 mb-1">
                  Рейтинг
                </label>
                <StarRating
                  value={form.user_rating}
                  onChange={(v) => setForm((f) => ({ ...f, user_rating: v }))}
                />
              </div>

              <div>
                <label className="block text-xs text-slate-400 mb-1">
                  Заметки
                </label>
                <textarea
                  value={form.user_notes}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, user_notes: e.target.value }))
                  }
                  rows={3}
                  className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500 resize-none"
                  placeholder="Комментарий о поставщике..."
                />
              </div>

              {createError && (
                <p className="text-xs text-red-400">{createError}</p>
              )}

              {duplicates.length > 0 && (
                <label className="flex items-center gap-2 text-xs text-slate-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={forceCreate}
                    onChange={(e) => setForceCreate(e.target.checked)}
                    className="accent-blue-500"
                  />
                  Создать всё равно (дубликат)
                </label>
              )}
            </div>

            <div className="px-6 py-4 border-t border-slate-700 flex justify-end gap-2">
              <button
                onClick={() => setShowCreate(false)}
                className="px-4 py-1.5 text-sm text-slate-400 hover:text-slate-200"
              >
                Отмена
              </button>
              <button
                onClick={handleCreate}
                disabled={
                  !form.name.trim() ||
                  creating ||
                  (duplicates.length > 0 && !forceCreate)
                }
                className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded transition-colors"
              >
                {creating ? "Создаю..." : "Создать"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function FormField({
  label,
  value,
  onChange,
  className = "",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className="block text-xs text-slate-400 mb-1">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
      />
    </div>
  );
}
