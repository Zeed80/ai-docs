"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
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

const API = `${getApiBaseUrl()}/api/agent/delegations`;

async function checked(response: Response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data));
  return data;
}

export default function DelegationsPage() {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [actions, setActions] = useState<{ name: string; effect: string }[]>([]);
  const [action, setAction] = useState("");
  const [title, setTitle] = useState("");
  const [constraints, setConstraints] = useState("{}");
  const [limit, setLimit] = useState(20);
  const [hours, setHours] = useState(2);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    const data = await checked(await fetch(API, { credentials: "include" }));
    setGrants(data.items);
  }, []);

  useEffect(() => {
    Promise.all([
      reload(),
      fetch(`${API}/actions`, { credentials: "include" }).then(checked).then(data => setActions(data.items)),
    ]).catch(e => setError(String(e.message ?? e))).finally(() => setLoading(false));
  }, [reload]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const scope = JSON.parse(constraints);
      if (!scope || Array.isArray(scope) || typeof scope !== "object" || !Object.keys(scope).length) {
        throw new Error("Укажите непустой JSON-объект с точными значениями аргументов.");
      }
      await checked(await mutFetch(API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, actions: [action], constraints: scope, max_actions: limit, duration_hours: hours }),
      }));
      await reload();
      setTitle("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }

  async function revoke(id: string) {
    setBusy(true);
    setError("");
    try {
      await checked(await mutFetch(`${API}/${id}`, { method: "DELETE" }));
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
            onChange={e => setAction(e.target.value)}>
            <option value="">Выберите действие</option>
            {actions.map(item => <option key={item.name} value={item.name}>{item.name} ({item.effect})</option>)}
          </select>
        </label>
        <label className="block">Точные ограничения аргументов (JSON)
          <textarea className="block w-full border rounded p-2 bg-transparent font-mono" rows={4} required
            aria-describedby="scope-help" value={constraints} onChange={e => setConstraints(e.target.value)} />
        </label>
        <p id="scope-help" className="text-sm">Например: {`{"invoice_id":"UUID конкретного счёта"}`}.
          Поля должны совпадать с аргументами выбранного действия. Шаблоны и исполняемые выражения не поддерживаются.</p>
        <label className="block">Максимум попыток (1–200)
          <input className="block border rounded p-2 bg-transparent" type="number" min={1} max={200} required
            value={limit} onChange={e => setLimit(Number(e.target.value))} />
        </label>
        <label className="block">Срок в часах (1–168)
          <input className="block border rounded p-2 bg-transparent" type="number" min={1} max={168} required
            value={hours} onChange={e => setHours(Number(e.target.value))} />
        </label>
        <button className="border rounded px-4 py-2 disabled:opacity-50" disabled={busy || !actions.length}>
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
