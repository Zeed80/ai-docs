"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useCallback, useEffect, useState } from "react";

const API = getApiBaseUrl();

type WorkOrder = {
  id: string; objective: string; description?: string; status: string; priority: number;
  risk_level: string; plan_revision: number; result_summary?: string; blocker?: Record<string, unknown>;
  created_at: string; updated_at: string;
};
type WorkStep = { id: string; step_key: string; title: string; kind: string; capability?: string;
  action?: string; depends_on: string[]; state: string; attempt_count: number; max_attempts: number;
  output?: Record<string, unknown>; last_error?: Record<string, unknown> };
type Event = { sequence: number; event_type: string; actor: string; payload: Record<string, unknown>; created_at: string };

const statusClass: Record<string, string> = {
  completed: "bg-emerald-900/50 text-emerald-300", blocked: "bg-red-950/60 text-red-300",
  failed: "bg-red-950/60 text-red-300", waiting_approval: "bg-amber-950/60 text-amber-300",
  running: "bg-blue-950/60 text-blue-300", planning: "bg-violet-950/60 text-violet-300",
  replanning: "bg-violet-950/60 text-violet-300", ready: "bg-slate-700 text-slate-200",
};

async function api(path: string, init?: RequestInit) {
  return fetch(`${API}${path}`, { credentials: "include", ...init,
    headers: { ...(init?.body ? { "Content-Type": "application/json", ...csrfHeaders() } : {}), ...init?.headers } });
}

export default function WorkOrdersPage() {
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [steps, setSteps] = useState<WorkStep[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [objective, setObjective] = useState("");
  const [description, setDescription] = useState("");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);

  const loadOrders = useCallback(async () => {
    const response = await api("/api/work-orders?limit=100");
    if (response.ok) setOrders(await response.json());
  }, []);
  const loadDetail = useCallback(async (id: string) => {
    const [planResponse, eventResponse] = await Promise.all([
      api(`/api/work-orders/${id}/plan`), api(`/api/work-orders/${id}/events?limit=100`),
    ]);
    setSteps(planResponse.ok ? (await planResponse.json()).steps : []);
    setEvents(eventResponse.ok ? await eventResponse.json() : []);
  }, []);

  useEffect(() => { loadOrders(); const timer = setInterval(loadOrders, 5000); return () => clearInterval(timer); }, [loadOrders]);
  useEffect(() => { if (!selected) return; loadDetail(selected); const timer = setInterval(() => loadDetail(selected), 5000); return () => clearInterval(timer); }, [selected, loadDetail]);

  async function createOrder() {
    if (!objective.trim()) return;
    setBusy(true);
    try {
      const response = await api("/api/work-orders", { method: "POST", body: JSON.stringify({ objective, description: description || null }) });
      if (response.ok) { const row = await response.json(); setObjective(""); setDescription(""); setSelected(row.id); await loadOrders(); }
    } finally { setBusy(false); }
  }
  async function act(action: "run" | "cancel") {
    if (!selected) return;
    await api(`/api/work-orders/${selected}/${action}`, { method: "POST", body: "{}" });
    await Promise.all([loadOrders(), loadDetail(selected)]);
  }
  async function addInstruction() {
    if (!selected || !instruction.trim()) return;
    await api(`/api/work-orders/${selected}/instructions`, { method: "POST", body: JSON.stringify({ instruction }) });
    setInstruction(""); await Promise.all([loadOrders(), loadDetail(selected)]);
  }

  const current = orders.find((order) => order.id === selected);
  return <div className="p-6 max-w-7xl mx-auto text-slate-100">
    <div className="flex items-end justify-between mb-5"><div><h1 className="text-2xl font-semibold">Поручения</h1>
      <p className="text-sm text-slate-400">Durable-планы, действия, проверки и восстановление автономного исполнителя</p></div>
      <span className="text-xs text-slate-500">обновление каждые 5 секунд</span></div>
    <div className="grid grid-cols-1 xl:grid-cols-[360px_1fr] gap-5">
      <section className="space-y-4">
        <div className="bg-slate-900 border border-slate-700 rounded-xl p-4 space-y-3">
          <input value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="Что нужно сделать?" className="w-full bg-slate-800 border border-slate-600 rounded px-3 py-2 text-sm" />
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Контекст, ограничения, ожидаемый результат" className="w-full h-20 bg-slate-800 border border-slate-600 rounded px-3 py-2 text-sm" />
          <button disabled={busy || !objective.trim()} onClick={createOrder} className="w-full rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 py-2 text-sm font-medium">Поставить поручение</button>
        </div>
        <div className="space-y-2 max-h-[65vh] overflow-auto">
          {orders.map((order) => <button key={order.id} onClick={() => setSelected(order.id)} className={`w-full text-left rounded-lg border p-3 ${selected === order.id ? "border-blue-500 bg-slate-800" : "border-slate-700 bg-slate-900 hover:border-slate-600"}`}>
            <div className="flex gap-2 justify-between"><span className="text-sm font-medium line-clamp-2">{order.objective}</span><span className={`h-fit shrink-0 px-2 py-0.5 rounded text-[10px] ${statusClass[order.status] || "bg-slate-700"}`}>{order.status}</span></div>
            <div className="mt-2 text-[11px] text-slate-500">rev {order.plan_revision} · priority {order.priority} · {new Date(order.created_at).toLocaleString("ru-RU")}</div>
          </button>)}
        </div>
      </section>
      <section className="min-w-0">
        {!current ? <div className="h-64 grid place-items-center text-slate-500 border border-dashed border-slate-700 rounded-xl">Выберите поручение</div> : <div className="space-y-4">
          <div className="bg-slate-900 border border-slate-700 rounded-xl p-5"><div className="flex justify-between gap-3"><div><h2 className="text-lg font-semibold">{current.objective}</h2><p className="text-sm text-slate-400 mt-1">{current.description}</p></div><span className={`h-fit px-2 py-1 rounded text-xs ${statusClass[current.status] || "bg-slate-700"}`}>{current.status}</span></div>
            {current.blocker && <pre className="mt-3 text-xs text-red-300 bg-red-950/30 rounded p-3 overflow-auto">{JSON.stringify(current.blocker, null, 2)}</pre>}
            {current.result_summary && <div className="mt-3 text-sm whitespace-pre-wrap bg-emerald-950/20 border border-emerald-900/40 rounded p-3">{current.result_summary}</div>}
            <div className="flex gap-2 mt-4"><button onClick={() => act("run")} className="px-3 py-1.5 rounded bg-blue-700 text-xs">Запустить следующий шаг</button><button onClick={() => act("cancel")} className="px-3 py-1.5 rounded bg-red-950 text-red-300 text-xs">Отменить</button></div>
            <div className="flex gap-2 mt-3"><input value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="Уточнить поручение и перепланировать" className="flex-1 bg-slate-800 border border-slate-600 rounded px-3 py-1.5 text-sm"/><button onClick={addInstruction} className="px-3 rounded bg-violet-700 text-xs">Добавить</button></div>
          </div>
          <div className="bg-slate-900 border border-slate-700 rounded-xl p-4"><h3 className="font-medium mb-3">План</h3><div className="space-y-2">{steps.map((step) => <div key={step.id} className="border border-slate-700 rounded-lg p-3"><div className="flex justify-between gap-3"><div><div className="text-sm font-medium">{step.title}</div><div className="text-xs text-slate-500 mt-1">{step.step_key} · {step.capability ? `${step.capability}.${step.action}` : step.kind} · depends: {step.depends_on.join(", ") || "—"}</div></div><span className={`text-xs h-fit px-2 py-0.5 rounded ${statusClass[step.state] || "bg-slate-700"}`}>{step.state}</span></div>{step.last_error && <pre className="text-xs text-red-300 mt-2 overflow-auto">{JSON.stringify(step.last_error, null, 2)}</pre>}</div>)}</div></div>
          <div className="bg-slate-900 border border-slate-700 rounded-xl p-4"><h3 className="font-medium mb-3">Журнал</h3><div className="space-y-2 max-h-72 overflow-auto">{events.slice().reverse().map((event) => <div key={event.sequence} className="grid grid-cols-[54px_180px_1fr] gap-2 text-xs border-b border-slate-800 pb-2"><span className="text-slate-500">#{event.sequence}</span><span className="text-blue-300">{event.event_type}</span><span className="text-slate-400 truncate" title={JSON.stringify(event.payload)}>{event.actor} · {JSON.stringify(event.payload)}</span></div>)}</div></div>
        </div>}
      </section>
    </div>
  </div>;
}
