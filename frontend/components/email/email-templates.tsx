"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";

type TemplateCategory =
  | "payment"
  | "inquiry"
  | "confirmation"
  | "reminder"
  | "request"
  | "custom";

const CATEGORY_LABELS: Record<TemplateCategory, string> = {
  payment: "Оплата",
  inquiry: "Запросы",
  confirmation: "Подтверждения",
  reminder: "Напоминания",
  request: "Запросы документов",
  custom: "Свои",
};

interface TemplateOut {
  id: string;
  name: string;
  slug: string;
  category: TemplateCategory;
  language: string;
  subject: string;
  body_html: string;
  body_text: string | null;
  variables: string[] | null;
  is_builtin: boolean;
  source_email_id: string | null;
  use_count: number;
}

interface TemplateForm {
  name: string;
  category: TemplateCategory;
  subject: string;
  body_html: string;
  body_text: string;
  variables: string;
}

const EMPTY_FORM: TemplateForm = {
  name: "",
  category: "custom",
  subject: "",
  body_html: "",
  body_text: "",
  variables: "",
};

const inputCls =
  "w-full rounded border border-slate-600 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500";

export function EmailTemplatesSection() {
  const [templates, setTemplates] = useState<TemplateOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterCat, setFilterCat] = useState<TemplateCategory | "all">("all");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<TemplateForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<TemplateOut | null>(null);
  const [fromEmailId, setFromEmailId] = useState("");
  const [showFromEmail, setShowFromEmail] = useState(false);
  const [fromEmailName, setFromEmailName] = useState("");
  const [creatingFromEmail, setCreatingFromEmail] = useState(false);

  const base = getApiBaseUrl();

  async function load() {
    setLoading(true);
    try {
      const res = await fetch(`${base}/api/email-templates`);
      if (res.ok) setTemplates(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  function openNew() {
    setEditing(null);
    setForm(EMPTY_FORM);
    setShowForm(true);
  }

  function openEdit(tpl: TemplateOut) {
    if (tpl.is_builtin) return;
    setEditing(tpl.id);
    setForm({
      name: tpl.name,
      category: tpl.category,
      subject: tpl.subject,
      body_html: tpl.body_html,
      body_text: tpl.body_text || "",
      variables: (tpl.variables || []).join(", "),
    });
    setShowForm(true);
  }

  async function save() {
    setSaving(true);
    try {
      const body = {
        name: form.name,
        category: form.category,
        subject: form.subject,
        body_html: form.body_html,
        body_text: form.body_text || undefined,
        variables: form.variables
          ? form.variables
              .split(",")
              .map((v) => v.trim())
              .filter(Boolean)
          : [],
      };
      const res = editing
        ? await fetch(`${base}/api/email-templates/${editing}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          })
        : await fetch(`${base}/api/email-templates`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
      if (res.ok) {
        setShowForm(false);
        await load();
      }
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: string) {
    if (!confirm("Удалить шаблон?")) return;
    await fetch(`${base}/api/email-templates/${id}`, { method: "DELETE" });
    await load();
  }

  async function createFromEmail() {
    if (!fromEmailId.trim() || !fromEmailName.trim()) return;
    setCreatingFromEmail(true);
    try {
      const res = await fetch(`${base}/api/email-templates/from-message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email_id: fromEmailId.trim(),
          name: fromEmailName.trim(),
          extract_variables: true,
        }),
      });
      if (res.ok) {
        setShowFromEmail(false);
        setFromEmailId("");
        setFromEmailName("");
        await load();
      }
    } finally {
      setCreatingFromEmail(false);
    }
  }

  const displayed =
    filterCat === "all"
      ? templates
      : templates.filter((t) => t.category === filterCat);

  const f = (key: keyof TemplateForm, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="rounded-xl border border-slate-700 bg-slate-800 p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">
            Шаблоны писем
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Готовые шаблоны для типовых ситуаций
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowFromEmail(true)}
            className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 text-sm rounded-lg"
          >
            Из письма
          </button>
          <button
            onClick={openNew}
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg"
          >
            + Создать
          </button>
        </div>
      </div>

      {/* Category filter */}
      <div className="flex flex-wrap gap-1.5">
        <button
          onClick={() => setFilterCat("all")}
          className={`px-2.5 py-1 text-xs rounded-full ${filterCat === "all" ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-300 hover:bg-slate-600"}`}
        >
          Все ({templates.length})
        </button>
        {(Object.keys(CATEGORY_LABELS) as TemplateCategory[]).map((cat) => {
          const count = templates.filter((t) => t.category === cat).length;
          if (count === 0) return null;
          return (
            <button
              key={cat}
              onClick={() => setFilterCat(cat)}
              className={`px-2.5 py-1 text-xs rounded-full ${filterCat === cat ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-300 hover:bg-slate-600"}`}
            >
              {CATEGORY_LABELS[cat]} ({count})
            </button>
          );
        })}
      </div>

      {loading ? (
        <p className="text-slate-500 text-sm">Загрузка...</p>
      ) : displayed.length === 0 ? (
        <p className="text-slate-500 text-sm">Нет шаблонов</p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {displayed.map((tpl) => (
            <div
              key={tpl.id}
              className="rounded-lg border border-slate-700 bg-slate-900 p-3 space-y-1"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="text-sm font-medium text-slate-200 truncate">
                      {tpl.name}
                    </span>
                    {tpl.is_builtin && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-blue-900/40 text-blue-400">
                        встроенный
                      </span>
                    )}
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-700 text-slate-400">
                      {CATEGORY_LABELS[tpl.category]}
                    </span>
                  </div>
                  <p className="text-xs text-slate-500 truncate mt-0.5">
                    {tpl.subject}
                  </p>
                </div>
                <div className="flex gap-1 shrink-0">
                  <button
                    onClick={() => setPreview(tpl)}
                    className="px-2 py-0.5 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded"
                  >
                    Просмотр
                  </button>
                  {!tpl.is_builtin && (
                    <>
                      <button
                        onClick={() => openEdit(tpl)}
                        className="px-2 py-0.5 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded"
                      >
                        Изм.
                      </button>
                      <button
                        onClick={() => remove(tpl.id)}
                        className="px-2 py-0.5 text-xs bg-red-900/30 hover:bg-red-900/50 text-red-400 rounded"
                      >
                        ✕
                      </button>
                    </>
                  )}
                </div>
              </div>
              {tpl.variables && tpl.variables.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {tpl.variables.slice(0, 5).map((v) => (
                    <span
                      key={v}
                      className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-400 font-mono"
                    >
                      {"{"}
                      {v}
                      {"}"}
                    </span>
                  ))}
                  {tpl.variables.length > 5 && (
                    <span className="text-[10px] text-slate-500">
                      +{tpl.variables.length - 5}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Create from email dialog */}
      {showFromEmail && (
        <div className="rounded-lg border border-slate-600 bg-slate-900 p-4 space-y-3">
          <h4 className="text-sm font-semibold text-slate-200">
            Создать шаблон из письма
          </h4>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              ID письма
            </label>
            <input
              className={inputCls}
              value={fromEmailId}
              onChange={(e) => setFromEmailId(e.target.value)}
              placeholder="UUID письма из базы"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Название шаблона
            </label>
            <input
              className={inputCls}
              value={fromEmailName}
              onChange={(e) => setFromEmailName(e.target.value)}
              placeholder="Мой шаблон"
            />
          </div>
          <div className="flex gap-2">
            <button
              onClick={createFromEmail}
              disabled={
                creatingFromEmail ||
                !fromEmailId.trim() ||
                !fromEmailName.trim()
              }
              className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-lg"
            >
              {creatingFromEmail ? "Создание..." : "Создать"}
            </button>
            <button
              onClick={() => setShowFromEmail(false)}
              className="px-4 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 text-sm rounded-lg"
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {/* Template form */}
      {showForm && (
        <div className="rounded-lg border border-slate-600 bg-slate-900 p-4 space-y-3">
          <h4 className="text-sm font-semibold text-slate-200">
            {editing ? "Редактировать шаблон" : "Новый шаблон"}
          </h4>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Название
              </label>
              <input
                className={inputCls}
                value={form.name}
                onChange={(e) => f("name", e.target.value)}
                placeholder="Напоминание об оплате"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Категория
              </label>
              <select
                className={inputCls}
                value={form.category}
                onChange={(e) => f("category", e.target.value)}
              >
                {(Object.keys(CATEGORY_LABELS) as TemplateCategory[]).map(
                  (cat) => (
                    <option key={cat} value={cat}>
                      {CATEGORY_LABELS[cat]}
                    </option>
                  ),
                )}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">Тема</label>
            <input
              className={inputCls}
              value={form.subject}
              onChange={(e) => f("subject", e.target.value)}
              placeholder="Напоминание: счёт №{invoice_number}"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Тело письма (HTML)
            </label>
            <textarea
              className={inputCls + " min-h-[160px] font-mono text-xs"}
              value={form.body_html}
              onChange={(e) => f("body_html", e.target.value)}
              placeholder="<p>Добрый день, {contact_name}!</p>"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Переменные (через запятую)
            </label>
            <input
              className={inputCls}
              value={form.variables}
              onChange={(e) => f("variables", e.target.value)}
              placeholder="invoice_number, contact_name, total_amount"
            />
            <p className="text-[10px] text-slate-500 mt-0.5">
              Оставьте пустым — переменные будут определены автоматически из{" "}
              {"{...}"}
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={save}
              disabled={
                saving || !form.name || !form.subject || !form.body_html
              }
              className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-lg"
            >
              {saving ? "Сохранение..." : "Сохранить"}
            </button>
            <button
              onClick={() => setShowForm(false)}
              className="px-4 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 text-sm rounded-lg"
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {/* Preview modal */}
      {preview && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-900 border border-slate-700 rounded-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
              <span className="font-semibold text-slate-200">
                {preview.name}
              </span>
              <button
                onClick={() => setPreview(null)}
                className="text-slate-400 hover:text-slate-200 text-xl"
              >
                ×
              </button>
            </div>
            <div className="overflow-y-auto p-4 space-y-3 text-sm">
              <div>
                <span className="text-xs text-slate-500">Тема:</span>
                <p className="text-slate-200 mt-0.5">{preview.subject}</p>
              </div>
              {preview.variables && preview.variables.length > 0 && (
                <div>
                  <span className="text-xs text-slate-500">Переменные:</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {preview.variables.map((v) => (
                      <span
                        key={v}
                        className="text-[11px] px-2 py-0.5 rounded bg-slate-700 text-slate-300 font-mono"
                      >
                        {"{"}
                        {v}
                        {"}"}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <div>
                <span className="text-xs text-slate-500">Содержимое:</span>
                <div
                  className="mt-1 text-slate-300 bg-slate-800 rounded p-3 text-xs leading-relaxed"
                  dangerouslySetInnerHTML={{ __html: preview.body_html }}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
