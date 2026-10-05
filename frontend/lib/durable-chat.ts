"use client";

import { mutFetch } from "@/lib/auth";
import { getActiveWorkspaceContext } from "@/lib/workspace-context";

type Event = Record<string, unknown>;
export type DurableConfirmation = {
  attempt_id: string; sha256: string; confirmation: {tool: string; args: Record<string, unknown>};
};
/**
 * A separate, server-issued continuation boundary for an action whose recipient
 * commit was independently verified.  This is deliberately not inferred from
 * ActionJournal/E09 observations: the resume endpoint rechecks it atomically.
 */
export type DurableVerifiedCommitContinuation = {
  intent: "verified_commit";
  attempt_id: string;
  action_id: string;
  sha256: string;
};
export type DurableContinuation = DurableConfirmation | DurableVerifiedCommitContinuation;

function isVerifiedCommitContinuation(value: unknown): value is DurableVerifiedCommitContinuation {
  if (!value || typeof value !== "object") return false;
  const checkpoint = value as Record<string, unknown>;
  return checkpoint.intent === "verified_commit"
    && typeof checkpoint.attempt_id === "string"
    && typeof checkpoint.action_id === "string"
    && typeof checkpoint.sha256 === "string";
}

function isConfirmation(value: unknown): value is DurableConfirmation {
  if (!value || typeof value !== "object") return false;
  const checkpoint = value as Record<string, unknown>;
  const confirmation = checkpoint.confirmation;
  return checkpoint.intent !== "verified_commit"
    && typeof checkpoint.attempt_id === "string"
    && typeof checkpoint.sha256 === "string"
    && !!confirmation
    && typeof confirmation === "object"
    && typeof (confirmation as Record<string, unknown>).tool === "string"
    && !!(confirmation as Record<string, unknown>).args
    && typeof (confirmation as Record<string, unknown>).args === "object";
}
type Run = {
  id: string; session_id: string; work_order_id: string; request_id: string;
  status: string; result_message_id: string | null; blocker: unknown;
};
const terminal = new Set(["completed", "blocked", "failed", "canceled"]);
// The deterministic verifier parks a finished turn as "blocked" until the
// independent semantic verifier decides; that is a wait, not an outcome.
const awaitingVerification = (run: Run) =>
  run.status === "blocked"
  && (run.blocker as {code?: unknown} | null)?.code === "independent_verification_required";

export function buildDurableUserCommand(
  content: string,
  sessionId?: string | null,
  attachments?: Array<{
    document_id: string;
    file_name: string;
    mime_type?: string;
    size_bytes?: number;
  }>,
  reasoningMode?: "normal" | "strict",
): Record<string, unknown> {
  return {
    type: "message",
    content,
    session_id: sessionId ?? undefined,
    workspace_context: getActiveWorkspaceContext(),
    attachments:
      attachments && attachments.length > 0 ? attachments : undefined,
    reasoning_mode: reasoningMode ?? "normal",
  };
}

/** Compatibility with the panel's command interface, without a WebSocket lifecycle. */
export class DurableChatTransport {
  readyState = 1;
  private generation = 0;
  private session: string | null = null;
  private run: Run | null = null;
  private busy = false;
  private pendingCancel = false;
  private cursor = 0;
  private verifyingNotified = false;
  private confirmation: DurableContinuation | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(private emit: (event: Event) => void) {}

  get isOpen(): boolean {
    return this.readyState === 1;
  }

  private async request(path: string, body?: Event): Promise<unknown> {
    const response = await mutFetch(path, body ? {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    } : { method: "GET", cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    return response.json();
  }

  close() {
    this.readyState = 3;
    this.generation++;
    clearTimeout(this.timer);
    // Closing the view never cancels the worker.
  }

  async watchSession(session: string, savedMessageIds: Set<string> = new Set()) {
    if (this.session === session || this.readyState !== 1) return;
    this.session = session;
    this.busy = false;
    this.pendingCancel = false;
    this.run = null;
    this.emit({type: "durable_run", run_id: null});
    this.cursor = 0;
    this.verifyingNotified = false;
    this.confirmation = null;
    this.emit({type: "durable_confirmation", checkpoint: null});
    this.emit({type: "durable_decision_pending", pending: false});
    this.emit({type: "durable_state", active: false});
    const generation = ++this.generation;
    clearTimeout(this.timer);
    try {
      const data = await this.request(`/api/agent/chat-runs?session_id=${encodeURIComponent(session)}`) as {run: Run | null; legacy: boolean};
      if (generation !== this.generation) return;
      this.emit({type: "durable_mode", legacy: data.legacy});
      if (data.legacy) {
        this.emit({type: "status", content: "Архивный чат доступен для чтения. Создайте новый чат для долговечного исполнения."});
      } else if (data.run) {
        this.run = data.run;
        this.emit({type: "durable_run", run_id: data.run.id});
        this.busy = !terminal.has(data.run.status);
        this.emit({type: "durable_state", active: this.busy});
        void this.poll(generation, 0, savedMessageIds);
      }
    } catch {
      if (generation === this.generation) {
        this.session = null;
        this.emit({type: "status", content: "Не удалось восстановить состояние задачи. Повторяю подключение…"});
        this.timer = setTimeout(() => void this.watchSession(session, savedMessageIds), 2000);
      }
    }
  }

  send(raw: string) {
    const message = JSON.parse(raw) as Event;
    if (message.type === "stop") { void this.cancel(); return; }
    if (message.type === "resume") {
      if (!this.busy && this.confirmation && typeof message.approved === "boolean") void this.resume(message.approved);
      return;
    }
    if (message.type !== "message") {
      this.emit({type: "error", content: "Устаревшая команда подтверждения отклонена. Действие не выполнено; используйте сохранённую карточку задачи."});
      return;
    }
    if (this.busy) { this.emit({type: "durable_state", active: true}); return; }
    void this.submit(message);
  }

  private async submit(message: Event) {
    this.busy = true;
    this.run = null;
    this.emit({type: "durable_run", run_id: null});
    this.pendingCancel = false;
    this.cursor = 0;
    this.verifyingNotified = false;
    this.confirmation = null;
    this.emit({type: "durable_confirmation", checkpoint: null});
    const generation = ++this.generation;
    clearTimeout(this.timer);
    const requestId = crypto.randomUUID();
    const body = {request_id: requestId, session_id: message.session_id,
      content: message.content, reasoning_mode: message.reasoning_mode ?? "normal",
      attachments: message.attachments ?? [], workspace_context: message.workspace_context ?? {}};
    try {
      let run: Run;
      try {
        run = await this.request("/api/agent/chat-runs", body) as Run;
      } catch (error) {
        if (String(error).includes("HTTP 4")) throw error;
        // Same logical request ID on a transport retry, never a new task.
        run = await this.request("/api/agent/chat-runs", body) as Run;
      }
      if (generation !== this.generation) return;
      this.run = run;
      this.session = run.session_id;
      this.emit({type: "durable_run", run_id: run.id});
      this.emit({type: "session", session_id: run.session_id});
      if (this.pendingCancel) await this.cancel();
      void this.poll(generation, 0, new Set());
    } catch (error) {
      if (generation !== this.generation) return;
      this.busy = false;
      this.emit({type: "error", content: `Приём запроса не подтверждён. Перед повтором проверьте историю задач. ${String(error)}`});
      this.emit({type: "done"});
      // Reconcile server state, never blindly send a new logical request.
      this.session = null;
      if (typeof message.session_id === "string") void this.watchSession(message.session_id);
    }
  }

  private async resume(approved: boolean) {
    if (!this.run || !this.confirmation) return;
    const runId = this.run.id;
    const continuation = this.confirmation;
    const verifiedCommit = isVerifiedCommitContinuation(continuation);
    const body = verifiedCommit
      ? {intent: "verified_commit" as const, action_id: continuation.action_id,
        attempt_id: continuation.attempt_id, sha256: continuation.sha256, approved}
      : {attempt_id: continuation.attempt_id, sha256: continuation.sha256, approved};
    const generation = ++this.generation;
    clearTimeout(this.timer);
    this.busy = true;
    this.pendingCancel = false;
    this.emit({type: "durable_decision_pending", pending: true});
    this.emit({type: "durable_state", active: true});
    try {
      let run: Run;
      if (verifiedCommit) {
        // A verified-commit decision is one-use.  A dropped reply or a stale
        // binding must be reconciled by GET, never blindly submitted again.
        run = await this.request(`/api/agent/chat-runs/${runId}/resume`, body) as Run;
      } else {
        try {
          run = await this.request(`/api/agent/chat-runs/${runId}/resume`, body) as Run;
        } catch (error) {
          if (String(error).includes("HTTP 4")) throw error;
          run = await this.request(`/api/agent/chat-runs/${runId}/resume`, body) as Run;
        }
      }
      if (generation !== this.generation) return;
      this.run = run;
      this.confirmation = null;
      this.busy = !terminal.has(run.status);
      this.emit({type: "durable_confirmation", checkpoint: null});
      this.emit({type: "durable_state", active: this.busy});
      if (this.pendingCancel) await this.cancel();
      void this.poll(generation, this.cursor, new Set());
    } catch (error) {
      if (generation !== this.generation) return;
      this.confirmation = null;
      this.emit({type: "durable_confirmation", checkpoint: null});
      const stale = String(error).includes("HTTP 409");
      const context = verifiedCommit
        ? (stale
          ? "Привязка проверенного результата устарела или больше небезопасна. Продолжение не выполнено и не будет повторно отправлено."
          : "Продолжение после проверенного результата не подтверждено. Запрос не будет повторно отправлен.")
        : "Решение не подтверждено.";
      this.emit({type: "status", content: `${context} Проверяю состояние задачи…`});
      // Reconcile an ambiguous reply without resubmitting a decision.
      void this.poll(generation, this.cursor, new Set());
    } finally {
      if (generation === this.generation) this.emit({type: "durable_decision_pending", pending: false});
    }
  }

  private async cancel() {
    if (!this.run) { this.pendingCancel = true; return; }
    try {
      await this.request(`/api/work-orders/${this.run.work_order_id}/cancel`, {});
    } catch (error) {
      this.emit({type: "status", content: `Отмена не подтверждена: ${String(error)}`});
    }
  }

  private async poll(generation: number, cursor: number, saved: Set<string>) {
    if (generation !== this.generation || !this.run || this.readyState !== 1) return;
    try {
      const run = await this.request(`/api/agent/chat-runs/${this.run.id}`) as Run;
      const page = await this.request(`/api/agent/chat-runs/${run.id}/events?after=${cursor}&limit=100`) as {
        items: {sequence: number; type: string; payload: {event?: Event}}[]; next_cursor: number;
      };
      if (generation !== this.generation) return;
      this.run = run;
      for (const item of page.items) {
        if (item.sequence <= cursor) continue;
        const event = item.payload.event;
        if (event && event.type !== "done" && !(event.type === "text" && run.result_message_id && saved.has(run.result_message_id))) {
          if (event.type === "confirmation_required") this.emit({type: "status", content: "Действие остановлено до подтверждения владельцем."});
          else this.emit(event);
        }
        cursor = item.sequence;
      }
      this.cursor = cursor;
      if (awaitingVerification(run)) {
        if (!this.verifyingNotified) {
          this.verifyingNotified = true;
          this.emit({type: "status", content: "Проверяю результат…"});
        }
      } else if (terminal.has(run.status) && page.items.length < 100) {
        let checkpoint: DurableContinuation | null = null;
        if (run.status === "blocked") {
          try {
            const state = await this.request(`/api/agent/chat-runs/${run.id}/checkpoint`) as {can_resume: boolean};
            if (state.can_resume && (isConfirmation(state) || isVerifiedCommitContinuation(state))) checkpoint = state;
          } catch (error) {
            if (!String(error).includes("HTTP 409")) throw error;
          }
          if (generation !== this.generation) return;
        }
        this.confirmation = checkpoint;
        this.emit({type: "durable_confirmation", checkpoint});
        this.busy = false;
        if (run.status !== "completed") this.emit({type: "status", content: `Задача: ${run.status}. ${run.blocker ? JSON.stringify(run.blocker) : ""}`});
        this.emit({type: "done"});
        return;
      }
      this.busy = true;
      this.confirmation = null;
      this.emit({type: "durable_confirmation", checkpoint: null});
      this.emit({type: "durable_state", active: true});
    } catch {
      if (generation !== this.generation) return;
      this.emit({type: "status", content: "Связь с задачей прервана; worker продолжает работу. Переподключаюсь…"});
    }
    if (generation === this.generation) this.timer = setTimeout(() => void this.poll(generation, cursor, saved), 1500);
  }
}
