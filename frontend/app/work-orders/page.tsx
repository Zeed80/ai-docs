"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useCallback, useEffect, useMemo, useState } from "react";

const API = getApiBaseUrl();

type WorkOrder = {
  id: string;
  objective: string;
  description?: string;
  status: string;
  priority: number;
  risk_level: string;
  plan_revision: number;
  result_summary?: string;
  blocker?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};
type WorkStep = {
  id: string;
  step_key: string;
  title: string;
  kind: string;
  capability?: string;
  action?: string;
  depends_on: string[];
  state: string;
  attempt_count: number;
  max_attempts: number;
  output?: Record<string, unknown>;
  last_error?: Record<string, unknown>;
};
type ToolCall = {
  id: string;
  step_id: string;
  executor: string;
  capability?: string;
  action?: string;
  arguments?: Record<string, unknown>;
  risk_level: string;
  status: string;
  action_digest?: string;
  output?: Record<string, unknown>;
  error?: Record<string, unknown>;
};
type Event = {
  sequence: number;
  event_type: string;
  actor: string;
  payload: Record<string, unknown>;
  created_at: string;
};
type WorkLearning = {
  status: string;
  summary?: string;
  lessons: Record<string, unknown>[];
  provenance: Record<string, unknown>;
  memory_fact_id?: string;
  recipe_skill_id?: string;
  extraction_attempts: number;
  last_error?: Record<string, unknown>;
  processed_at?: string;
};
type PendingApproval = {
  id: string;
  status: string;
  context: Record<string, unknown> | null;
};
// Б14: aggregate operator-facing observability shown in the page header.
type Metrics = {
  window_hours: number;
  status_counts: Record<string, number>;
  step_durations: Record<
    string,
    { count: number; p50_seconds: number; p95_seconds: number }
  >;
};

const statusClass: Record<string, string> = {
  completed: "bg-emerald-900/50 text-emerald-300",
  blocked: "bg-red-950/60 text-red-300",
  failed: "bg-red-950/60 text-red-300",
  waiting_approval: "bg-amber-950/60 text-amber-300",
  running: "bg-blue-950/60 text-blue-300",
  planning: "bg-violet-950/60 text-violet-300",
  replanning: "bg-violet-950/60 text-violet-300",
  ready: "bg-slate-700 text-slate-200",
};
// Б13.4: a lightweight stand-in for a real DAG widget — steps at the same
// dependency depth share a color band, so the reader can see "these run in
// parallel, that one waits on them" without a graph library.
const levelBand = [
  "border-l-sky-500",
  "border-l-violet-500",
  "border-l-amber-500",
  "border-l-emerald-500",
  "border-l-rose-500",
  "border-l-cyan-500",
];

async function api(path: string, init?: RequestInit) {
  return fetch(`${API}${path}`, {
    credentials: "include",
    ...init,
    headers: {
      ...(init?.body
        ? { "Content-Type": "application/json", ...csrfHeaders() }
        : {}),
      ...init?.headers,
    },
  });
}

// Б13.4: depth of each step in the dependency DAG (0 = no deps), by step_key.
// depends_on references step_key (see WorkStep.depends_on in the backend
// schema), not step id — a cycle or a dangling reference degrades to 0
// instead of an infinite loop or a crash.
function stepLevels(steps: WorkStep[]): Record<string, number> {
  const byKey = new Map(steps.map((s) => [s.step_key, s]));
  const memo: Record<string, number> = {};
  const resolving = new Set<string>();
  function levelOf(key: string): number {
    if (key in memo) return memo[key];
    if (resolving.has(key)) return 0; // cycle guard
    resolving.add(key);
    const step = byKey.get(key);
    const deps = step?.depends_on ?? [];
    const level =
      deps.length === 0
        ? 0
        : 1 + Math.max(0, ...deps.map((d) => (byKey.has(d) ? levelOf(d) : 0)));
    resolving.delete(key);
    memo[key] = level;
    return level;
  }
  for (const s of steps) levelOf(s.step_key);
  return memo;
}

export default function WorkOrdersPage() {
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [steps, setSteps] = useState<WorkStep[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [learning, setLearning] = useState<WorkLearning | null>(null);
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>(
    [],
  );
  const [objective, setObjective] = useState("");
  const [description, setDescription] = useState("");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [expandedToolCalls, setExpandedToolCalls] = useState<Set<string>>(
    new Set(),
  );
  const [confirmForceRun, setConfirmForceRun] = useState(false);
  const [metrics, setMetrics] = useState<Metrics | null>(null);

  const loadOrders = useCallback(async () => {
    const response = await api("/api/work-orders?limit=100");
    if (response.ok) setOrders(await response.json());
  }, []);
  const loadMetrics = useCallback(async () => {
    const response = await api("/api/work-orders/metrics");
    if (response.ok) setMetrics(await response.json());
  }, []);
  const loadDetail = useCallback(async (id: string) => {
    const [
      planResponse,
      eventResponse,
      learningResponse,
      toolCallResponse,
      approvalResponse,
    ] = await Promise.all([
      api(`/api/work-orders/${id}/plan`),
      api(`/api/work-orders/${id}/events?limit=100`),
      api(`/api/work-orders/${id}/learning`),
      api(`/api/work-orders/${id}/tool-calls`),
      // Б13.3: scoped client-side — /api/approvals has no entity_id filter,
      // so pull the pending queue and keep only this work order's rows.
      api(`/api/approvals/pending?action_type=agent_tool_call&limit=200`),
    ]);
    setSteps(planResponse.ok ? (await planResponse.json()).steps : []);
    setEvents(eventResponse.ok ? await eventResponse.json() : []);
    setLearning(learningResponse.ok ? await learningResponse.json() : null);
    setToolCalls(toolCallResponse.ok ? await toolCallResponse.json() : []);
    if (approvalResponse.ok) {
      const body = await approvalResponse.json();
      const items: PendingApproval[] = body.items ?? [];
      setPendingApprovals(items.filter((a) => a.context?.work_order_id === id));
    } else {
      setPendingApprovals([]);
    }
  }, []);

  useEffect(() => {
    loadOrders();
    const timer = setInterval(loadOrders, 5000);
    return () => clearInterval(timer);
  }, [loadOrders]);
  useEffect(() => {
    loadMetrics();
    const timer = setInterval(loadMetrics, 30000);
    return () => clearInterval(timer);
  }, [loadMetrics]);
  useEffect(() => {
    if (!selected) return;
    loadDetail(selected);
    const timer = setInterval(() => loadDetail(selected), 5000);
    return () => clearInterval(timer);
  }, [selected, loadDetail]);

  const levels = useMemo(() => stepLevels(steps), [steps]);
  const toolCallsByStep = useMemo(() => {
    const map = new Map<string, ToolCall[]>();
    for (const call of toolCalls) {
      const list = map.get(call.step_id) ?? [];
      list.push(call);
      map.set(call.step_id, list);
    }
    return map;
  }, [toolCalls]);
  const approvalByStep = useMemo(() => {
    const map = new Map<string, PendingApproval>();
    for (const approval of pendingApprovals) {
      const stepId = approval.context?.step_id as string | undefined;
      if (stepId) map.set(stepId, approval);
    }
    return map;
  }, [pendingApprovals]);

  async function createOrder() {
    if (!objective.trim()) return;
    setBusy(true);
    try {
      const response = await api("/api/work-orders", {
        method: "POST",
        body: JSON.stringify({ objective, description: description || null }),
      });
      if (response.ok) {
        const row = await response.json();
        setObjective("");
        setDescription("");
        setSelected(row.id);
        await loadOrders();
      }
    } finally {
      setBusy(false);
    }
  }
  async function act(action: "run" | "cancel") {
    if (!selected) return;
    await api(`/api/work-orders/${selected}/${action}`, {
      method: "POST",
      body: "{}",
    });
    await Promise.all([loadOrders(), loadDetail(selected)]);
  }
  async function addInstruction() {
    if (!selected || !instruction.trim()) return;
    await api(`/api/work-orders/${selected}/instructions`, {
      method: "POST",
      body: JSON.stringify({ instruction }),
    });
    setInstruction("");
    await Promise.all([loadOrders(), loadDetail(selected)]);
  }
  async function reprocessLearning() {
    if (!selected) return;
    await api(`/api/work-orders/${selected}/learning/reprocess`, {
      method: "POST",
      body: "{}",
    });
    await loadDetail(selected);
  }
  // Б13.3: inline approve/reject — same /api/approvals/{id}/decide the
  // standalone /approvals page uses, wired directly into the step card so
  // an operator never has to leave this view to unblock a work order.
  async function decideApproval(
    approvalId: string,
    decision: "approved" | "rejected",
  ) {
    await api(`/api/approvals/${approvalId}/decide`, {
      method: "POST",
      body: JSON.stringify({ status: decision }),
    });
    if (selected) await Promise.all([loadOrders(), loadDetail(selected)]);
  }
  function toggleToolCall(id: string) {
    setExpandedToolCalls((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const current = orders.find((order) => order.id === selected);
  return (
    <div className="p-6 max-w-7xl mx-auto text-slate-100">
      <div className="flex items-end justify-between mb-5">
        <div>
          <h1 className="text-2xl font-semibold">Поручения</h1>
          <p className="text-sm text-slate-400">
            Durable-планы, действия, проверки и восстановление автономного
            исполнителя
          </p>
        </div>
        <span className="text-xs text-slate-500">
          обновление каждые 5 секунд
        </span>
      </div>
      {metrics && (
        <div className="mb-5 flex flex-wrap gap-3 text-xs">
          <span className="text-slate-500">за {metrics.window_hours}ч:</span>
          {Object.entries(metrics.status_counts).map(([status, count]) => (
            <span
              key={status}
              className={`px-2 py-0.5 rounded ${statusClass[status] || "bg-slate-700"}`}
            >
              {status}: {count}
            </span>
          ))}
          {Object.entries(metrics.step_durations).map(([key, d]) => (
            <span
              key={key}
              className="px-2 py-0.5 rounded bg-slate-800 text-slate-400"
            >
              {key} p50 {d.p50_seconds}с / p95 {d.p95_seconds}с ({d.count})
            </span>
          ))}
        </div>
      )}
      <div className="grid grid-cols-1 xl:grid-cols-[360px_1fr] gap-5">
        <section className="space-y-4">
          <div className="bg-slate-900 border border-slate-700 rounded-xl p-4 space-y-3">
            <input
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              placeholder="Что нужно сделать?"
              className="w-full bg-slate-800 border border-slate-600 rounded px-3 py-2 text-sm"
            />
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Контекст, ограничения, ожидаемый результат"
              className="w-full h-20 bg-slate-800 border border-slate-600 rounded px-3 py-2 text-sm"
            />
            <button
              disabled={busy || !objective.trim()}
              onClick={createOrder}
              className="w-full rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 py-2 text-sm font-medium"
            >
              Поставить поручение
            </button>
          </div>
          <div className="space-y-2 max-h-[65vh] overflow-auto">
            {orders.map((order) => (
              <button
                key={order.id}
                onClick={() => setSelected(order.id)}
                className={`w-full text-left rounded-lg border p-3 ${selected === order.id ? "border-blue-500 bg-slate-800" : "border-slate-700 bg-slate-900 hover:border-slate-600"}`}
              >
                <div className="flex gap-2 justify-between">
                  <span className="text-sm font-medium line-clamp-2">
                    {order.objective}
                  </span>
                  <span
                    className={`h-fit shrink-0 px-2 py-0.5 rounded text-[10px] ${statusClass[order.status] || "bg-slate-700"}`}
                  >
                    {order.status}
                  </span>
                </div>
                <div className="mt-2 text-[11px] text-slate-500">
                  rev {order.plan_revision} · priority {order.priority} ·{" "}
                  {new Date(order.created_at).toLocaleString("ru-RU")}
                </div>
              </button>
            ))}
          </div>
        </section>
        <section className="min-w-0">
          {!current ? (
            <div className="h-64 grid place-items-center text-slate-500 border border-dashed border-slate-700 rounded-xl">
              Выберите поручение
            </div>
          ) : (
            <div className="space-y-4">
              <div className="bg-slate-900 border border-slate-700 rounded-xl p-5">
                <div className="flex justify-between gap-3">
                  <div>
                    <h2 className="text-lg font-semibold">
                      {current.objective}
                    </h2>
                    <p className="text-sm text-slate-400 mt-1">
                      {current.description}
                    </p>
                  </div>
                  <span
                    className={`h-fit px-2 py-1 rounded text-xs ${statusClass[current.status] || "bg-slate-700"}`}
                  >
                    {current.status}
                  </span>
                </div>
                {current.blocker && (
                  <pre className="mt-3 text-xs text-red-300 bg-red-950/30 rounded p-3 overflow-auto">
                    {JSON.stringify(current.blocker, null, 2)}
                  </pre>
                )}
                {current.result_summary && (
                  <div className="mt-3 text-sm whitespace-pre-wrap bg-emerald-950/20 border border-emerald-900/40 rounded p-3">
                    {current.result_summary}
                  </div>
                )}
                {/* Б13.1: the autonomous dispatcher (work.dispatch_ready, Celery beat)
                is what's supposed to advance ready steps on its own — this button
                is a manual override for debugging/unsticking, not the normal path.
                Labelled and gated behind a confirm so it reads as an escape hatch,
                not "how you make things happen here". */}
                <div className="flex gap-2 mt-4">
                  {confirmForceRun ? (
                    <>
                      <span className="text-xs text-amber-300 self-center">
                        Обычно шаги идут сами (автономный dispatcher).
                        Форсировать сейчас?
                      </span>
                      <button
                        onClick={() => {
                          setConfirmForceRun(false);
                          act("run");
                        }}
                        className="px-3 py-1.5 rounded bg-amber-700 text-xs"
                      >
                        Да, форсировать
                      </button>
                      <button
                        onClick={() => setConfirmForceRun(false)}
                        className="px-3 py-1.5 rounded bg-slate-700 text-xs"
                      >
                        Отмена
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => setConfirmForceRun(true)}
                      className="px-3 py-1.5 rounded bg-slate-700 text-slate-300 text-xs"
                    >
                      Форсировать шаг вручную (debug)
                    </button>
                  )}
                  <button
                    onClick={() => act("cancel")}
                    className="px-3 py-1.5 rounded bg-red-950 text-red-300 text-xs"
                  >
                    Отменить
                  </button>
                </div>
                <div className="flex gap-2 mt-3">
                  <input
                    value={instruction}
                    onChange={(e) => setInstruction(e.target.value)}
                    placeholder="Уточнить поручение и перепланировать"
                    className="flex-1 bg-slate-800 border border-slate-600 rounded px-3 py-1.5 text-sm"
                  />
                  <button
                    onClick={addInstruction}
                    className="px-3 rounded bg-violet-700 text-xs"
                  >
                    Добавить
                  </button>
                </div>
              </div>
              <div className="bg-slate-900 border border-slate-700 rounded-xl p-4">
                <h3 className="font-medium mb-3">План</h3>
                <div className="space-y-2">
                  {steps.map((step) => {
                    const calls = toolCallsByStep.get(step.id) ?? [];
                    const approval = approvalByStep.get(step.id);
                    const band =
                      levelBand[
                        (levels[step.step_key] ?? 0) % levelBand.length
                      ];
                    return (
                      <div
                        key={step.id}
                        className={`border border-slate-700 border-l-4 ${band} rounded-lg p-3`}
                      >
                        <div className="flex justify-between gap-3">
                          <div>
                            <div className="text-sm font-medium">
                              {step.title}
                            </div>
                            <div className="text-xs text-slate-500 mt-1">
                              {step.step_key} · уровень{" "}
                              {levels[step.step_key] ?? 0} ·{" "}
                              {step.capability
                                ? `${step.capability}.${step.action}`
                                : step.kind}{" "}
                              · depends: {step.depends_on.join(", ") || "—"}
                            </div>
                          </div>
                          <span
                            className={`text-xs h-fit px-2 py-0.5 rounded ${statusClass[step.state] || "bg-slate-700"}`}
                          >
                            {step.state}
                          </span>
                        </div>
                        {step.last_error && (
                          <pre className="text-xs text-red-300 mt-2 overflow-auto">
                            {JSON.stringify(step.last_error, null, 2)}
                          </pre>
                        )}
                        {/* Б13.3: inline approve/reject on the exact gated step. */}
                        {step.state === "waiting_approval" && approval && (
                          <div className="mt-2 bg-amber-950/20 border border-amber-900/40 rounded p-2">
                            <div className="text-xs text-amber-200">
                              Требует подтверждения:{" "}
                              <span className="font-mono">
                                {String(approval.context?.tool_name ?? "")}.
                                {String(approval.context?.action ?? "")}
                              </span>
                            </div>
                            <pre className="text-[11px] text-amber-100/80 mt-1 overflow-auto max-h-24">
                              {JSON.stringify(
                                approval.context?.tool_args ?? {},
                                null,
                                2,
                              )}
                            </pre>
                            <div className="flex gap-2 mt-2">
                              <button
                                onClick={() =>
                                  decideApproval(approval.id, "approved")
                                }
                                className="px-2 py-1 rounded bg-emerald-800 text-emerald-100 text-xs"
                              >
                                Одобрить
                              </button>
                              <button
                                onClick={() =>
                                  decideApproval(approval.id, "rejected")
                                }
                                className="px-2 py-1 rounded bg-red-900 text-red-200 text-xs"
                              >
                                Отклонить
                              </button>
                            </div>
                          </div>
                        )}
                        {/* Б13.2: the write-ahead evidence the durable-runtime design is
                    built around — args, digest, result — previously invisible
                    in this view entirely. */}
                        {calls.length > 0 && (
                          <div className="mt-2 space-y-1">
                            {calls.map((call) => (
                              <div key={call.id} className="text-xs">
                                <button
                                  onClick={() => toggleToolCall(call.id)}
                                  className="text-slate-400 hover:text-slate-200"
                                >
                                  {expandedToolCalls.has(call.id) ? "▾" : "▸"}{" "}
                                  tool_call · {call.status} · digest{" "}
                                  {call.action_digest?.slice(0, 10) ?? "—"}
                                </button>
                                {expandedToolCalls.has(call.id) && (
                                  <div className="mt-1 grid grid-cols-1 lg:grid-cols-2 gap-2">
                                    <pre className="bg-slate-950/50 rounded p-2 overflow-auto max-h-32">
                                      {JSON.stringify(
                                        call.arguments ?? {},
                                        null,
                                        2,
                                      )}
                                    </pre>
                                    <pre className="bg-slate-950/50 rounded p-2 overflow-auto max-h-32">
                                      {JSON.stringify(
                                        call.output ?? call.error ?? {},
                                        null,
                                        2,
                                      )}
                                    </pre>
                                  </div>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
              {learning && (
                <div className="bg-slate-900 border border-slate-700 rounded-xl p-4">
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="font-medium">Память исполнения</h3>
                    <div className="flex items-center gap-2">
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${learning.status === "recorded" ? "bg-emerald-900/50 text-emerald-300" : learning.status === "failed" ? "bg-red-950/60 text-red-300" : "bg-violet-950/60 text-violet-300"}`}
                      >
                        {learning.status}
                      </span>
                      <button
                        onClick={reprocessLearning}
                        className="px-2 py-1 rounded bg-slate-700 text-xs"
                      >
                        Пересобрать
                      </button>
                    </div>
                  </div>
                  {learning.summary && (
                    <p className="mt-3 text-sm text-slate-300 whitespace-pre-wrap">
                      {learning.summary}
                    </p>
                  )}
                  <div className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-3">
                    <pre className="text-xs bg-slate-950/50 rounded p-3 overflow-auto">
                      {JSON.stringify(learning.lessons, null, 2)}
                    </pre>
                    <pre className="text-xs bg-slate-950/50 rounded p-3 overflow-auto">
                      {JSON.stringify(learning.provenance, null, 2)}
                    </pre>
                  </div>
                  <div className="mt-2 text-[11px] text-slate-500">
                    memory {learning.memory_fact_id || "—"} · recipe{" "}
                    {learning.recipe_skill_id || "не создан"} · попыток{" "}
                    {learning.extraction_attempts}
                  </div>
                  {learning.last_error && (
                    <pre className="mt-2 text-xs text-red-300 overflow-auto">
                      {JSON.stringify(learning.last_error, null, 2)}
                    </pre>
                  )}
                </div>
              )}
              <div className="bg-slate-900 border border-slate-700 rounded-xl p-4">
                <h3 className="font-medium mb-3">Журнал</h3>
                <div className="space-y-2 max-h-72 overflow-auto">
                  {events
                    .slice()
                    .reverse()
                    .map((event) => (
                      <div
                        key={event.sequence}
                        className="grid grid-cols-[54px_180px_1fr] gap-2 text-xs border-b border-slate-800 pb-2"
                      >
                        <span className="text-slate-500">
                          #{event.sequence}
                        </span>
                        <span className="text-blue-300">
                          {event.event_type}
                        </span>
                        <span
                          className="text-slate-400 truncate"
                          title={JSON.stringify(event.payload)}
                        >
                          {event.actor} · {JSON.stringify(event.payload)}
                        </span>
                      </div>
                    ))}
                </div>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
