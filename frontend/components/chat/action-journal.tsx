"use client";

import { useEffect, useRef, useState } from "react";
import { mutFetch } from "@/lib/auth";

type Observation = { sequence?: number; request_id: string; actor?: string; created_at?: string; outcome: string; note: string; evidence_reference: string; verified?: false; can_replay?: false };
type Action = { id: string; tool: string; status: string; request_digest: string; latest_observation: Observation | null };
type Page = { items: Action[]; next_offset: number; work_order_status: string };
type ObservationPage = { items: Observation[]; next_cursor: number };
type Detail = { id: string; status: string; request: unknown; result: unknown; request_digest: string; result_digest: string | null;
  recipient_receipt?: { operation: string; response: unknown; response_digest: string; evidence_scope: string } | null };
type Submission = { request_id: string; request_digest: string; outcome: string; note: string; evidence_reference: string };
type Verification = { action_id: string; status: "matched" | "changed" | "missing" | "inconclusive"; observed_at: string; scope: string;
  can_replay: false; can_resume: false; reason?: string };
const labels: Record<string, string> = {
  planned: "Не начато", started: "Вызов начат", waiting_confirmation: "Ожидает подтверждения",
  result_recorded: "Ответ сохранён — эффект не проверен", outcome_unknown: "Исход неизвестен",
};
const outcomes: Record<string, string> = {observed: "Эффект обнаружен", not_observed: "Эффект не обнаружен", inconclusive: "Недостаточно данных"};
const verificationLabels: Record<Verification["status"], string> = {
  matched: "Совпадает на момент сверки",
  changed: "Изменилось с момента квитанции",
  missing: "Объект больше не найден",
  inconclusive: "Недостаточно данных для вывода",
};

async function request<T>(path: string, signal: AbortSignal, body?: Submission): Promise<T> {
  const response = await mutFetch(path, {method: body ? "POST" : "GET", signal, cache: "no-store",
    ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

export function ActionJournal({runId}: {runId: string}) {
  const [page, setPage] = useState<Page | null>(null);
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<Action | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setPage(null); setSelected(null); setError("");
    request<Page>(`/api/agent/chat-runs/${encodeURIComponent(runId)}/actions?offset=${offset}&limit=20`, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setPage(data); })
      .catch((err) => { if (!controller.signal.aborted) setError(`Не удалось прочитать журнал: ${String(err)}`); });
    return () => controller.abort();
  }, [runId, offset, revision]);
  return <section className="space-y-4" aria-label="Журнал действий">
    <p className="break-all text-xs">Запуск: {runId}</p>
    <p>Журнал содержит только действия, записанные новым worker. Отсутствие записи не доказывает отсутствие внешнего эффекта.</p>
    <p>Наблюдения человека не проверены автоматически и не разрешают повтор действия.</p>
    <button type="button" onClick={() => setRevision((n) => n + 1)} className="rounded border px-3 py-2">Обновить журнал</button>
    {error && <p role="alert">{error}</p>}
    {!page && !error && <p role="status">Загрузка журнала…</p>}
    {page && <>
      <p>Состояние работы: {page.work_order_status}</p>
      {page.items.length === 0 && <p>На этой странице нет записанных действий.</p>}
      <ul className="space-y-2">{page.items.map((action) => <li key={action.id}>
        <button type="button" aria-pressed={selected?.id === action.id} onClick={() => setSelected(action)} className="w-full rounded border p-3 text-left">
          <span className="block break-all">{action.tool} — {labels[action.status] ?? action.status}</span>
          <span className="block break-all text-xs">{action.id}</span>
        </button>
      </li>)}</ul>
      <nav aria-label="Страницы журнала" className="flex gap-3">
        <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Назад</button>
        <button type="button" disabled={page.items.length < 20} onClick={() => setOffset(page.next_offset)}>Далее</button>
      </nav>
      {selected && <ActionDetail key={`${runId}:${selected.id}`} runId={runId} action={selected} stopped={["blocked", "failed", "canceled"].includes(page.work_order_status)} />}
    </>}
  </section>;
}

function ActionDetail({runId, action, stopped}: {runId: string; action: Action; stopped: boolean}) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [outcome, setOutcome] = useState("inconclusive");
  const [note, setNote] = useState("");
  const [reference, setReference] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Submission | null>(null);
  const [saved, setSaved] = useState<Observation | null>(action.latest_observation);
  const [history, setHistory] = useState<Observation[]>([]);
  const [historyCursor, setHistoryCursor] = useState(0);
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyMore, setHistoryMore] = useState(true);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [verificationError, setVerificationError] = useState("");
  const [verifying, setVerifying] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const verificationController = useRef<AbortController | null>(null);
  const verificationKey = useRef(0);
  const historyController = useRef<AbortController | null>(null);
  const historyKey = useRef(0);
  const mounted = useRef(true);
  const submitting = useRef(false);
  const path = `/api/agent/chat-runs/${encodeURIComponent(runId)}/actions/${encodeURIComponent(action.id)}`;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      verificationKey.current += 1;
      verificationController.current?.abort();
      historyKey.current += 1;
      historyController.current?.abort();
    };
  }, []);
  useEffect(() => {
    const current = new AbortController(); controller.current = current;
    request<Detail>(path, current.signal).then((data) => {
      if (!current.signal.aborted) setDetail(data);
    }).catch((err) => { if (!current.signal.aborted) setError(`Не удалось проверить запись: ${String(err)}`); });
    return () => current.abort();
  }, [path]);
  async function submit() {
    if (!detail || !controller.current || submitting.current) return;
    const signal = controller.current.signal;
    const body = pending ?? {request_id: crypto.randomUUID(), request_digest: detail.request_digest,
      outcome, note: note.trim(), evidence_reference: reference.trim()};
    if (!body.note || !body.evidence_reference) return;
    submitting.current = true; setBusy(true); setPending(body); setError("");
    try {
      const result = await request<Observation>(`${path}/observations`, signal, body);
      if (signal.aborted) return;
      setSaved(result); setPending(null); setNote(""); setReference("");
    } catch (err) {
      if (!signal.aborted) setError(`Сохранение не подтверждено: ${String(err)}. Повтор отправит то же наблюдение с тем же идентификатором.`);
    } finally {
      submitting.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  async function verifyCurrentState() {
    verificationController.current?.abort();
    const current = new AbortController();
    verificationController.current = current;
    const requestKey = verificationKey.current + 1;
    verificationKey.current = requestKey;
    setVerification(null); setVerificationError(""); setVerifying(true);
    try {
      const result = await request<Verification>(`${path}/verification`, current.signal);
      if (current.signal.aborted || !mounted.current || verificationKey.current !== requestKey) return;
      setVerification(result);
    } catch (err) {
      if (current.signal.aborted || !mounted.current || verificationKey.current !== requestKey) return;
      const message = String(err);
      if (message.includes("HTTP 403")) setVerificationError("Текущий доступ администратора для сверки отсутствует.");
      else if (message.includes("HTTP 409")) setVerificationError("Квитанция или запись действия нарушает проверку целостности.");
      else setVerificationError(`Не удалось проверить текущее состояние: ${message}`);
    } finally {
      if (!current.signal.aborted && mounted.current && verificationKey.current === requestKey) setVerifying(false);
    }
  }
  async function loadObservationHistory() {
    if (historyLoading || !historyMore) return;
    historyController.current?.abort();
    const current = new AbortController();
    historyController.current = current;
    const requestKey = historyKey.current + 1;
    historyKey.current = requestKey;
    setHistoryError(""); setHistoryLoading(true);
    try {
      const result = await request<ObservationPage>(`${path}/observations?cursor=${historyCursor}&limit=20`, current.signal);
      if (current.signal.aborted || !mounted.current || historyKey.current !== requestKey) return;
      setHistory((previous) => {
        const bySequence = new Map(previous.map((observation) => [observation.sequence, observation]));
        for (const observation of result.items) {
          if (observation.sequence !== saved?.sequence) bySequence.set(observation.sequence, observation);
        }
        return [...bySequence.values()].sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0));
      });
      setHistoryCursor(result.next_cursor);
      setHistoryMore(result.items.length === 20 && result.next_cursor !== historyCursor);
    } catch (err) {
      if (!current.signal.aborted && mounted.current && historyKey.current === requestKey) {
        setHistoryError(`Не удалось прочитать предыдущие наблюдения: ${String(err)}`);
      }
    } finally {
      if (!current.signal.aborted && mounted.current && historyKey.current === requestKey) setHistoryLoading(false);
    }
  }
  return <section aria-label="Сверка действия" className="space-y-3 rounded border p-4">
    <h2 className="font-semibold">Сверка действия</h2>
    {error && <p role="alert">{error}</p>}
    {!detail && !error && <p role="status">Проверка целостности записи…</p>}
    {detail && <>
      <p>{labels[detail.status] ?? detail.status}</p>
      <p className="break-all text-xs">Хеш запроса: {detail.request_digest}</p>
      <h3>Точные аргументы</h3><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(detail.request, null, 2)}</pre>
      <h3>Ответ worker</h3><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{detail.result_digest ? JSON.stringify(detail.result, null, 2) : "Результат не сохранён"}</pre>
      {detail.recipient_receipt && <section aria-label="Квитанция получателя" className="space-y-1">
        <h3>Квитанция получателя</h3>
        <p>Операция: {detail.recipient_receipt.operation}</p>
        <p>Подтверждена запись в БД в момент выполнения. Это не проверка текущего состояния объекта и не подтверждение внешней доставки. Повтор и продолжение не разрешены автоматически.</p>
        <p className="break-all text-xs">Хеш ответа: {detail.recipient_receipt.response_digest}</p>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(detail.recipient_receipt.response, null, 2)}</pre>
        <section aria-label="Текущая сверка" aria-live="polite" className="space-y-1 rounded border p-3">
          <h4>Текущая сверка</h4>
          <p>Сверка читает текущее состояние отдельно от квитанции прошлого commit и не выполняет действие.</p>
          <button type="button" className="rounded border px-3 py-2" disabled={verifying} onClick={() => { void verifyCurrentState(); }}>{verifying ? "Проверка…" : "Проверить текущее состояние"}</button>
          {verificationError && <p role="alert">{verificationError}</p>}
          {verification && <>
            <p>Статус: {verificationLabels[verification.status]}</p>
            <p className="break-all">Время наблюдения: {verification.observed_at}</p>
            <p className="break-all">Область сверки: {verification.scope}</p>
            {verification.reason && <p>Причина: {verification.reason}</p>}
            <p>Повтор и продолжение по результату сверки не разрешены.</p>
          </>}
        </section>
      </section>}
      {saved && <section aria-label="Последнее наблюдение" aria-live="polite" className="space-y-1">
        <h3>Последнее наблюдение — не проверено</h3>
        <p>{outcomes[saved.outcome] ?? saved.outcome}</p><p className="whitespace-pre-wrap break-words">{saved.note}</p>
        <p className="break-all">Источник: {saved.evidence_reference}</p>
        {saved.actor && <p>Автор: {saved.actor}</p>}
        {saved.created_at && <p>Время: {saved.created_at}</p>}
        <button type="button" className="rounded border px-3 py-2" disabled={historyLoading || !historyMore} onClick={() => { void loadObservationHistory(); }}>
          {historyLoading ? "Загрузка наблюдений…" : history.length ? "Показать ещё предыдущие наблюдения" : "Показать предыдущие наблюдения"}
        </button>
        {historyError && <p role="alert">{historyError}</p>}
        {history.length > 0 && <section aria-label="Предыдущие наблюдения" className="space-y-2 rounded border p-3">
          <h4>Предыдущие наблюдения — не проверены</h4>
          {history.map((observation) => <article key={observation.sequence} className="space-y-1 border-b pb-2 last:border-0">
            <p>{outcomes[observation.outcome] ?? observation.outcome}</p>
            <p className="whitespace-pre-wrap break-words">{observation.note}</p>
            <p className="break-all">Источник: {observation.evidence_reference}</p>
            {observation.actor && <p>Автор: {observation.actor}</p>}
            {observation.created_at && <p>Время: {observation.created_at}</p>}
          </article>)}
        </section>}
      </section>}
      {stopped && detail.status === "outcome_unknown" && <form className="space-y-3" onSubmit={(event) => {event.preventDefault(); void submit();}}>
        <label className="block">Наблюдаемый исход<select className="block w-full rounded border p-2" value={outcome} disabled={busy || !!pending} onChange={(event) => setOutcome(event.target.value)}>
          {Object.entries(outcomes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label className="block">Обоснование<textarea className="block w-full rounded border p-2" required maxLength={10000} value={note} disabled={busy || !!pending} onChange={(event) => setNote(event.target.value)} /></label>
        <label className="block">Ссылка или идентификатор свидетельства<input className="block w-full rounded border p-2" required maxLength={2000} value={reference} disabled={busy || !!pending} onChange={(event) => setReference(event.target.value)} /></label>
        <p>Источник сохраняется как текст и не открывается автоматически. Состояние работы не изменится; это не разрешение повторить действие.</p>
        <button className="rounded border px-3 py-2" type="submit" disabled={busy || (!pending && (!note.trim() || !reference.trim()))}>{busy ? "Сохранение…" : pending ? "Повторить сохранение наблюдения" : "Сохранить наблюдение"}</button>
      </form>}
    </>}
  </section>;
}
