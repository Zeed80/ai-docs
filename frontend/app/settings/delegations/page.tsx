"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";

interface Grant {
  id: string;
  title: string;
  actions: string[];
  constraints: Record<string, unknown>;
  max_actions: number;
  used_actions: number;
  expires_at: string;
  revoked_at: string | null;
}

interface Field {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  format: string | null;
  required: boolean;
}
interface DelegableAction {
  name: string;
  effect: string;
  fields: Field[];
}

const API = getApiBaseUrl();

// E46: a value typed as the field says; "" means "not pinned".
function typedValue(field: Field, raw: string): unknown {
  if (raw.trim() === "") return undefined;
  if (field.type === "boolean") return raw === "true";
  if (field.type === "integer") return Number.parseInt(raw, 10);
  if (field.type === "number") return Number(raw);
  return raw.trim();
}

function fieldHint(field: Field): string {
  if (field.format === "uuid") return "идентификатор (UUID)";
  if (field.format === "sha256") return "SHA-256, 64 hex";
  return { string: "текст", integer: "целое число", number: "число", boolean: "да/нет" }[field.type];
}
const DELEGATIONS = `${API}/api/agent/delegations`;

async function checked(response: Response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data));
  return data;
}

export default function DelegationsPage() {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [actions, setActions] = useState<DelegableAction[]>([]);
  const [action, setAction] = useState("");
  const [title, setTitle] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [limit, setLimit] = useState(20);
  const [hours, setHours] = useState(2);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    const data = await checked(await fetch(DELEGATIONS, { credentials: "include" }));
    setGrants(data.items);
  }, []);

  useEffect(() => {
    Promise.all([
      reload(),
      fetch(`${DELEGATIONS}/actions`, { credentials: "include" }).then(checked).then(data => setActions(data.items)),
    ]).catch(e => setError(String(e.message ?? e))).finally(() => setLoading(false));
  }, [reload]);

  const selected = actions.find((item) => item.name === action);
  const scope = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const field of selected?.fields ?? []) {
      const value = typedValue(field, values[field.name] ?? "");
      if (value !== undefined && !(typeof value === "number" && Number.isNaN(value))) {
        out[field.name] = value;
      }
    }
    return out;
  }, [selected, values]);
  const until = new Date(Date.now() + hours * 3600_000).toLocaleString("ru-RU");
  const summary = selected
    ? `Агент сможет выполнить «${selected.name}» без повторного подтверждения, только если ` +
      (Object.keys(scope).length
        ? Object.entries(scope).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(" и ")
        : "…(укажите хотя бы одно поле)") +
      `; не больше ${limit} раз, до ${until}.`
    : "";

  async function create(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (!Object.keys(scope).length) {
        throw new Error("Укажите хотя бы одно поле: без точного ограничения разрешение не выдаётся.");
      }
      // The server checks the names, types and values again (E46).
      await checked(await mutFetch(DELEGATIONS, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, actions: [action], constraints: scope, max_actions: limit, duration_hours: hours }),
      }));
      await reload();
      setTitle("");
      setValues({});
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }

  async function revoke(id: string) {
    setBusy(true);
    setError("");
    try {
      await checked(await mutFetch(`${DELEGATIONS}/${id}`, { method: "DELETE" }));
      await reload();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return <main className="max-w-3xl mx-auto p-6 space-y-6">
    <Link href="/settings" className="underline">Настройки</Link>
    <h1 className="text-2xl font-bold">Разрешения агенту</h1>
    <p>Агент выполняет указанные действия без повторного подтверждения только при точном совпадении ограничений.
      Разрешение не расширяет ваши права. Неудачная попытка также расходует лимит.
      Отзыв запрещает новые вызовы, но не отменяет уже начатое действие.</p>
    {error && <p role="alert" className="text-red-500 break-words">{error}</p>}
    {loading ? <p role="status">Загрузка…</p> : <>
      <form onSubmit={create} className="border rounded-lg p-4 space-y-4">
        <h2 className="font-semibold">Выдать ограниченное разрешение</h2>
        <label className="block">Название
          <input className="block w-full border rounded p-2 bg-transparent" required maxLength={300}
            value={title} onChange={e => setTitle(e.target.value)} />
        </label>
        <label className="block">Действие
          <select className="block w-full border rounded p-2 bg-background" required value={action}
            onChange={e => { setAction(e.target.value); setValues({}); }}>
            <option value="">Выберите действие</option>
            {actions.map(item => <option key={item.name} value={item.name}>{item.name} ({item.effect})</option>)}
          </select>
        </label>
        {selected && <fieldset className="border rounded p-3 space-y-3">
          <legend className="px-1">Точные значения аргументов</legend>
          <p id="scope-help" className="text-sm">Заполните поля, которые нужно закрепить; пустое поле не ограничивает.
            Значение сравнивается целиком — шаблоны и «любой» не поддерживаются.</p>
          {selected.fields.map(field => <label key={field.name} className="block">
            {field.name} <span className="text-sm opacity-70">— {fieldHint(field)}</span>
            {field.type === "boolean"
              ? <select className="block w-full border rounded p-2 bg-background" aria-describedby="scope-help"
                  value={values[field.name] ?? ""}
                  onChange={e => setValues(v => ({ ...v, [field.name]: e.target.value }))}>
                  <option value="">не ограничивать</option>
                  <option value="true">да</option>
                  <option value="false">нет</option>
                </select>
              : <input className="block w-full border rounded p-2 bg-transparent font-mono" aria-describedby="scope-help"
                  inputMode={field.type === "string" ? "text" : "decimal"}
                  pattern={field.format === "uuid"
                    ? "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    : field.type === "integer" ? "-?[0-9]+" : undefined}
                  value={values[field.name] ?? ""}
                  onChange={e => setValues(v => ({ ...v, [field.name]: e.target.value }))} />}
          </label>)}
        </fieldset>}
        <label className="block">Максимум попыток (1–200)
          <input className="block border rounded p-2 bg-transparent" type="number" min={1} max={200} required
            value={limit} onChange={e => setLimit(Number(e.target.value))} />
        </label>
        <label className="block">Срок в часах (1–168)
          <input className="block border rounded p-2 bg-transparent" type="number" min={1} max={168} required
            value={hours} onChange={e => setHours(Number(e.target.value))} />
        </label>
        {selected && <div className="space-y-2" aria-live="polite">
          <p className="text-sm">{summary}</p>
          <pre className="text-xs whitespace-pre-wrap break-all border rounded p-2" aria-label="Итоговое разрешение">
            {JSON.stringify({ actions: [action], constraints: scope, max_actions: limit, duration_hours: hours }, null, 2)}
          </pre>
        </div>}
        <button className="border rounded px-4 py-2 disabled:opacity-50"
          disabled={busy || !actions.length || !selected || !Object.keys(scope).length}>
          Выдать разрешение на указанных условиях
        </button>
      </form>
      <h2 className="font-semibold">Мои разрешения</h2>
      {!grants.length && <p>Разрешений пока нет.</p>}
      {grants.map(grant => <section key={grant.id} className="border rounded-lg p-4 space-y-2">
        <h3 className="font-semibold">{grant.title}</h3>
        <p className="break-all">{grant.actions.join(", ")}</p>
        <pre className="text-sm whitespace-pre-wrap break-all">{JSON.stringify(grant.constraints, null, 2)}</pre>
        <p>Попытки: {grant.used_actions} / {grant.max_actions}. До: {new Date(grant.expires_at).toLocaleString("ru-RU")}.</p>
        {grant.revoked_at ? <p>Отозвано</p> : <button className="border rounded px-3 py-1"
          disabled={busy} onClick={() => revoke(grant.id)}>Отозвать</button>}
      </section>)}
    </>}
  </main>;
}
