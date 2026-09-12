"use client";

import { mutFetch } from "@/lib/auth";

type Event = Record<string, unknown>;
export type DurableConfirmation = {
  attempt_id: string; sha256: string; confirmation: {tool: string; args: Record<string, unknown>};
};
type Run = {
  id: string; session_id: string; work_order_id: string; request_id: string;
  status: string; result_message_id: string | null; blocker: unknown;
};
const terminal = new Set(["completed", "blocked", "failed", "canceled"]);

/** Compatibility with the panel's command interface, without a WebSocket lifecycle. */
export class DurableChatTransport {
  readyState = 1;
  private generation = 0;
  private session: string | null = null;
  private run: Run | null = null;
  private busy = false;
  private pendingCancel = false;
  private cursor = 0;
  private confirmation: DurableConfirmation | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(private emit: (event: Event) => void) {}

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
    this.cursor = 0;
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
      this.emit({type: "error", content: "Продолжение после подтверждения пока недоступно. Действие не выполнено."});
      return;
    }
    if (this.busy) { this.emit({type: "durable_state", active: true}); return; }
    void this.submit(message);
  }

  private async submit(message: Event) {
    this.busy = true;
    this.run = null;
    this.pendingCancel = false;
    this.cursor = 0;
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
    const body = {attempt_id: this.confirmation.attempt_id, sha256: this.confirmation.sha256, approved};
    const generation = ++this.generation;
    clearTimeout(this.timer);
    this.busy = true;
    this.pendingCancel = false;
    this.emit({type: "durable_decision_pending", pending: true});
    this.emit({type: "durable_state", active: true});
    try {
      let run: Run;
      try {
        run = await this.request(`/api/agent/chat-runs/${runId}/resume`, body) as Run;
      } catch (error) {
        if (String(error).includes("HTTP 4")) throw error;
        run = await this.request(`/api/agent/chat-runs/${runId}/resume`, body) as Run;
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
      this.emit({type: "status", content: `Решение не подтверждено: ${String(error)}. Проверяю состояние задачи…`});
      // Reconcile an ambiguous reply without resubmitting a different decision.
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
      if (terminal.has(run.status) && page.items.length < 100) {
        let checkpoint: DurableConfirmation | null = null;
        if (run.status === "blocked") {
          try {
            const state = await this.request(`/api/agent/chat-runs/${run.id}/checkpoint`) as DurableConfirmation & {can_resume: boolean};
            if (state.can_resume) checkpoint = state;
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
