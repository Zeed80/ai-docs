"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";
import { tz } from "@/lib/user-time";
import { httpDetail, notifyError } from "@/components/ui/primitives/Toast";

const API = getApiBaseUrl();

interface Cond {
  field: string;
  op: string;
  value: string;
}
interface Action {
  type: string;
  label_id?: string;
  template_id?: string;
  folder?: string;
  address?: string;
  role?: string;
  prompt?: string;
}
interface LogEntry {
  at: string;
  message_subject: string | null;
  message_from: string | null;
  actions_applied: { type: string }[];
}
interface Rule {
  id: string;
  name: string;
  mailbox: string | null;
  is_active: boolean;
  priority: number;
  stop_processing: boolean;
  auto_send?: boolean;
  conditions: { match: "all" | "any"; rules: Cond[] };
  actions: Action[];
  run_count: number;
}
interface LabelOpt {
  id: string;
  name: string;
}
interface TemplateOpt {
  id: string;
  name: string;
}

const FIELDS = [
  "from",
  "to",
  "cc",
  "subject",
  "body",
  "sender_domain",
  "has_attachment",
  "attachment_name",
  "attachment_type",
  "is_from_known_supplier",
];
const OPS = [
  "contains",
  "not_contains",
  "equals",
  "starts_with",
  "ends_with",
  "matches_regex",
  "in_list",
  "is_true",
];
// Ф3: forward_to and assign_role were listed in the engine's docstring and had
// neither an implementation nor a control here.
const ACTION_TYPES = [
  "add_label",
  "move_to_folder",
  "mark_read",
  "star",
  "assign_role",
  "forward_to",
  "run_extraction",
  "forward_to_agent",
  "auto_reply_template",
  "stop",
];

const ACTION_LABELS: Record<string, string> = {
  add_label: "Навесить метку",
  move_to_folder: "Переместить в папку",
  mark_read: "Пометить прочитанным",
  star: "Пометить звёздочкой",
  assign_role: "Назначить ответственного (по роли)",
  forward_to: "Переслать на адрес",
  run_extraction: "Отправить вложения на распознавание",
  forward_to_agent: "Передать агенту как поручение",
  auto_reply_template: "Ответить по шаблону",
  stop: "Остановить обработку правил",
};

const ASSIGN_ROLES = [
  "accountant", "buyer", "manager", "engineer",
  "technologist", "normcontroller", "calculator",
];

export function EmailRulesSection() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [labels, setLabels] = useState<LabelOpt[]>([]);
  const [templates, setTemplates] = useState<TemplateOpt[]>([]);
  const [editing, setEditing] = useState<Rule | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  // Ф3: EmailRuleLog was written on every application and shown nowhere, so
  // "правило работает?" had no answer short of reading the database.
  const [log, setLog] = useState<Record<string, LogEntry[]>>({});

  const loadLog = (id: string) =>
    apiFetch(`${API}/api/email/rules/${id}/log?limit=10`)
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: LogEntry[]) =>
        setLog((prev) => ({ ...prev, [id]: Array.isArray(rows) ? rows : [] })),
      )
      .catch(() => {});

  const load = () => {
    apiFetch(`${API}/api/email/rules`)
      .then((r) => r.json())
      .then((d) => setRules(Array.isArray(d) ? d : []))
      .catch(() => setRules([]));
    apiFetch(`${API}/api/email/labels`)
      .then((r) => r.json())
      .then((d) => setLabels(Array.isArray(d) ? d : []))
      .catch(() => {});
    apiFetch(`${API}/api/email-templates/`)
      .then((r) => r.json())
      .then((d) => setTemplates(Array.isArray(d) ? d : (d.items ?? [])))
      .catch(() => {});
  };
  useEffect(load, []);

  const blank = (): Rule => ({
    id: "",
    name: "",
    mailbox: null,
    is_active: true,
    priority: 100,
    stop_processing: false,
    auto_send: false,
    conditions: { match: "all", rules: [{ field: "from", op: "contains", value: "" }] },
    actions: [{ type: "add_label" }],
    run_count: 0,
  });

  async function save() {
    if (!editing) return;
    const body = {
      name: editing.name,
      mailbox: editing.mailbox,
      conditions: editing.conditions,
      actions: editing.actions,
      priority: editing.priority,
      stop_processing: editing.stop_processing,
      is_active: editing.is_active,
      auto_send: !!editing.auto_send,
    };
    const res = editing.id
      ? await mutFetch(`${API}/api/email/rules/${editing.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
      : await mutFetch(`${API}/api/email/rules`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    if (res.ok) {
      setEditing(null);
      load();
    } else {
      notifyError(
        "Правило не сохранено",
        httpDetail(res.status, res.statusText),
      );
    }
  }

  async function dryRun(rule: Rule) {
    const res = await mutFetch(`${API}/api/email/rules/${rule.id}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_n: 20 }),
    });
    if (res.ok) {
      const d = await res.json();
      setTestResult((s) => ({ ...s, [rule.id]: `${d.matched} из ${d.total}` }));
    } else {
      notifyError(
        "Проверка правила не выполнена",
        httpDetail(res.status, res.statusText),
      );
    }
  }

  const inp =
    "px-2 py-1 text-xs bg-slate-700 border border-slate-600 text-slate-200 rounded";

  return (
    <section className="rounded-xl border border-slate-700 bg-slate-800 p-5">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-100">Правила обработки писем</h3>
          <p className="text-xs text-slate-400">
            Если письмо подходит под условия — выполнить действия при получении.
          </p>
        </div>
        <button
          onClick={() => setEditing(blank())}
          className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500"
        >
          + Правило
        </button>
      </div>

      <div className="space-y-2">
        {rules.length === 0 && <p className="text-xs text-slate-500">Правил нет</p>}
        {rules.map((r) => (
          <div
            key={r.id}
            className="rounded border border-slate-700 bg-slate-900/40 px-3 py-2 text-xs"
          >
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${r.is_active ? "bg-green-500" : "bg-slate-600"}`} />
            <span className="font-medium text-slate-200">{r.name}</span>
            <span className="text-slate-500">
              {r.conditions.rules.length} усл. → {r.actions.map((a) => a.type).join(", ")}
            </span>
            <span className="text-slate-600">· сработало {r.run_count}×</span>
            <span className="ml-auto flex gap-1">
              {testResult[r.id] && <span className="text-slate-400">{testResult[r.id]}</span>}
              <button onClick={() => dryRun(r)} className="text-slate-400 hover:text-slate-200">
                Проверить
              </button>
              <button
                onClick={() => (log[r.id] ? setLog((p) => {
                  const n = { ...p };
                  delete n[r.id];
                  return n;
                }) : loadLog(r.id))}
                className="text-slate-400 hover:text-slate-200"
              >
                Журнал
              </button>
              <button onClick={() => setEditing(r)} className="text-slate-400 hover:text-slate-200">
                Изм.
              </button>
              <button
                onClick={() =>
                  mutFetch(`${API}/api/email/rules/${r.id}`, { method: "DELETE" }).then(load)
                }
                className="text-slate-500 hover:text-red-400"
              >
                ✕
              </button>
            </span>
          </div>

          {log[r.id] && (
            <div className="mt-2 border-t border-slate-800 pt-2">
              {log[r.id].length === 0 ? (
                <p className="text-slate-500">
                  Правило ещё ни разу не срабатывало на реальных письмах.
                </p>
              ) : (
                <ul className="space-y-1">
                  {log[r.id].map((entry, idx) => (
                    <li key={idx} className="text-slate-400">
                      <span className="text-slate-500">
                        {new Date(entry.at).toLocaleString("ru-RU", { timeZone: tz() })}
                      </span>{" "}
                      · {entry.message_from ?? "—"} ·{" "}
                      {entry.message_subject ?? "(без темы)"} →{" "}
                      {entry.actions_applied
                        .map((a) => ACTION_LABELS[a.type] ?? a.type)
                        .join(", ")}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          </div>
        ))}
      </div>

      {editing && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-xl border border-slate-700 bg-slate-800 p-4">
            <input
              value={editing.name}
              onChange={(e) => setEditing({ ...editing, name: e.target.value })}
              placeholder="Название правила"
              className={`${inp} mb-3 w-full`}
            />

            <div className="mb-2 flex items-center gap-2 text-xs text-slate-300">
              Совпадение:
              <select
                value={editing.conditions.match}
                onChange={(e) =>
                  setEditing({
                    ...editing,
                    conditions: { ...editing.conditions, match: e.target.value as "all" | "any" },
                  })
                }
                className={inp}
              >
                <option value="all">все условия</option>
                <option value="any">любое условие</option>
              </select>
            </div>

            {editing.conditions.rules.map((c, i) => (
              <div key={i} className="mb-1.5 flex gap-1.5">
                <select
                  value={c.field}
                  onChange={(e) => {
                    const rules = [...editing.conditions.rules];
                    rules[i] = { ...c, field: e.target.value };
                    setEditing({ ...editing, conditions: { ...editing.conditions, rules } });
                  }}
                  className={inp}
                >
                  {FIELDS.map((f) => (
                    <option key={f}>{f}</option>
                  ))}
                </select>
                <select
                  value={c.op}
                  onChange={(e) => {
                    const rules = [...editing.conditions.rules];
                    rules[i] = { ...c, op: e.target.value };
                    setEditing({ ...editing, conditions: { ...editing.conditions, rules } });
                  }}
                  className={inp}
                >
                  {OPS.map((o) => (
                    <option key={o}>{o}</option>
                  ))}
                </select>
                {c.op !== "is_true" && (
                  <input
                    value={c.value}
                    onChange={(e) => {
                      const rules = [...editing.conditions.rules];
                      rules[i] = { ...c, value: e.target.value };
                      setEditing({ ...editing, conditions: { ...editing.conditions, rules } });
                    }}
                    className={`${inp} flex-1`}
                  />
                )}
                <button
                  onClick={() => {
                    const rules = editing.conditions.rules.filter((_, j) => j !== i);
                    setEditing({ ...editing, conditions: { ...editing.conditions, rules } });
                  }}
                  className="text-slate-500 hover:text-red-400"
                >
                  ✕
                </button>
              </div>
            ))}
            <button
              onClick={() =>
                setEditing({
                  ...editing,
                  conditions: {
                    ...editing.conditions,
                    rules: [...editing.conditions.rules, { field: "subject", op: "contains", value: "" }],
                  },
                })
              }
              className="mb-3 text-xs text-blue-400"
            >
              + условие
            </button>

            <p className="mb-1 text-xs font-medium text-slate-400">Действия</p>
            {editing.actions.map((a, i) => (
              <div key={i} className="mb-1.5 flex gap-1.5">
                <select
                  value={a.type}
                  onChange={(e) => {
                    const actions = [...editing.actions];
                    actions[i] = { type: e.target.value };
                    setEditing({ ...editing, actions });
                  }}
                  className={inp}
                >
                  {ACTION_TYPES.map((x) => (
                    <option key={x} value={x}>
                      {ACTION_LABELS[x] ?? x}
                    </option>
                  ))}
                </select>
                {a.type === "add_label" && (
                  <select
                    value={a.label_id ?? ""}
                    onChange={(e) => {
                      const actions = [...editing.actions];
                      actions[i] = { ...a, label_id: e.target.value };
                      setEditing({ ...editing, actions });
                    }}
                    className={inp}
                  >
                    <option value="">— метка —</option>
                    {labels.map((l) => (
                      <option key={l.id} value={l.id}>
                        {l.name}
                      </option>
                    ))}
                  </select>
                )}
                {a.type === "auto_reply_template" && (
                  <select
                    value={a.template_id ?? ""}
                    onChange={(e) => {
                      const actions = [...editing.actions];
                      actions[i] = { ...a, template_id: e.target.value };
                      setEditing({ ...editing, actions });
                    }}
                    className={inp}
                  >
                    <option value="">— шаблон (черновик) —</option>
                    {templates.map((tpl) => (
                      <option key={tpl.id} value={tpl.id}>
                        {tpl.name}
                      </option>
                    ))}
                  </select>
                )}
                {a.type === "move_to_folder" && (
                  <input
                    value={a.folder ?? ""}
                    placeholder="archive / spam / …"
                    onChange={(e) => {
                      const actions = [...editing.actions];
                      actions[i] = { ...a, folder: e.target.value };
                      setEditing({ ...editing, actions });
                    }}
                    className={inp}
                  />
                )}
                {a.type === "assign_role" && (
                  <select
                    value={a.role ?? ""}
                    onChange={(e) => {
                      const actions = [...editing.actions];
                      actions[i] = { ...a, role: e.target.value };
                      setEditing({ ...editing, actions });
                    }}
                    className={inp}
                  >
                    <option value="">— выберите роль —</option>
                    {ASSIGN_ROLES.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                )}
                {a.type === "forward_to" && (
                  <input
                    value={a.address ?? ""}
                    placeholder="адрес получателя"
                    onChange={(e) => {
                      const actions = [...editing.actions];
                      actions[i] = { ...a, address: e.target.value };
                      setEditing({ ...editing, actions });
                    }}
                    className={`${inp} flex-1`}
                  />
                )}
                {a.type === "forward_to_agent" && (
                  <input
                    value={a.prompt ?? ""}
                    placeholder="что сделать агенту"
                    onChange={(e) => {
                      const actions = [...editing.actions];
                      actions[i] = { ...a, prompt: e.target.value };
                      setEditing({ ...editing, actions });
                    }}
                    className={`${inp} flex-1`}
                  />
                )}
                <button
                  onClick={() =>
                    setEditing({ ...editing, actions: editing.actions.filter((_, j) => j !== i) })
                  }
                  className="text-slate-500 hover:text-red-400"
                >
                  ✕
                </button>
              </div>
            ))}
            <button
              onClick={() =>
                setEditing({ ...editing, actions: [...editing.actions, { type: "star" }] })
              }
              className="mb-3 text-xs text-blue-400"
            >
              + действие
            </button>

            <label className="mb-2 flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={editing.stop_processing}
                onChange={(e) => setEditing({ ...editing, stop_processing: e.target.checked })}
              />
              не применять следующие правила
            </label>
            {editing.actions.some((a) => a.type === "auto_reply_template") && (
              <label className="mb-3 flex items-center gap-2 text-xs text-amber-300">
                <input
                  type="checkbox"
                  checked={!!editing.auto_send}
                  onChange={(e) => setEditing({ ...editing, auto_send: e.target.checked })}
                />
                отправлять автоответ без подтверждения (нужно включить «Авто-отправка» в
                Политике почты)
              </label>
            )}

            <div className="flex gap-2">
              <button
                onClick={save}
                className="rounded bg-blue-600 px-4 py-1.5 text-xs text-white hover:bg-blue-500"
              >
                Сохранить
              </button>
              <button
                onClick={() => setEditing(null)}
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
