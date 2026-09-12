import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DurableChatTransport } from "@/lib/durable-chat";
import { mutFetch } from "@/lib/auth";

vi.mock("@/lib/auth", () => ({ mutFetch: vi.fn() }));
const fetcher = vi.mocked(mutFetch);
const run = {id: "run", session_id: "session", work_order_id: "order", request_id: "request",
  status: "running", result_message_id: null, blocker: null};
const response = (body: unknown) => new Response(JSON.stringify(body), {status: 200});
const page = (items: unknown[] = []) => response({items, next_cursor: items.length});
const event = (sequence: number, type: string, content?: string) => ({sequence, type: `chat.${type}`, payload: {event: {type, content}}});
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
let transport: DurableChatTransport;
const emit = vi.fn<(event: Record<string, unknown>) => void>();

beforeEach(() => { vi.useFakeTimers(); fetcher.mockReset(); emit.mockReset(); transport = new DurableChatTransport(emit); });
afterEach(() => { transport.close(); vi.useRealTimers(); });

describe("долговечный транспорт чата", () => {
  const checkpoint = {can_resume: true, attempt_id: "attempt", sha256: "a".repeat(64),
    confirmation: {tool: "email.send", args: {message_id: "draft"}}};

  async function waiting() {
    const blocked = {...run, status: "blocked"};
    fetcher.mockResolvedValueOnce(response({run: blocked, legacy: false}))
      .mockResolvedValueOnce(response(blocked)).mockResolvedValueOnce(page([event(7, "text", "До подтверждения")]))
      .mockResolvedValueOnce(response(checkpoint));
    await transport.watchSession("session"); await flush();
  }

  it("восстанавливает карточку, но не принимает решение автоматически", async () => {
    await waiting();
    expect(emit).toHaveBeenCalledWith({type: "durable_confirmation", checkpoint});
    expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
  });

  it("повторяет точное решение и продолжает чтение с сохранённого cursor", async () => {
    await waiting();
    fetcher.mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(response(run)).mockResolvedValueOnce(response(run)).mockResolvedValueOnce(page());
    transport.send(JSON.stringify({type: "resume", approved: true}));
    transport.send(JSON.stringify({type: "resume", approved: true})); await flush();
    const posts = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0][0]).toBe("/api/agent/chat-runs/run/resume");
    expect(posts[0][1]?.body).toBe(posts[1][1]?.body);
    expect(JSON.parse(String(posts[0][1]?.body))).toEqual({attempt_id: "attempt", sha256: checkpoint.sha256, approved: true});
    expect(fetcher).toHaveBeenLastCalledWith("/api/agent/chat-runs/run/events?after=7&limit=100", expect.anything());
    expect(emit.mock.calls.filter(([e]) => e.type === "text")).toHaveLength(1);
  });

  it("передаёт отказ и убирает карточку без запуска", async () => {
    await waiting();
    const blocked = {...run, status: "blocked"};
    fetcher.mockResolvedValueOnce(response(blocked)).mockResolvedValueOnce(response(blocked))
      .mockResolvedValueOnce(page()).mockResolvedValueOnce(response({can_resume: false}));
    transport.send(JSON.stringify({type: "resume", approved: false})); await flush();
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(emit).toHaveBeenCalledWith({type: "durable_confirmation", checkpoint: null});
    expect(emit).toHaveBeenLastCalledWith({type: "done"});
  });

  it("не предлагает продолжение при неизвестном внешнем эффекте", async () => {
    const blocked = {...run, status: "blocked"};
    fetcher.mockResolvedValueOnce(response({run: blocked, legacy: false}))
      .mockResolvedValueOnce(response(blocked)).mockResolvedValueOnce(page())
      .mockResolvedValueOnce(response({can_resume: false, phase: "tool_started"}));
    await transport.watchSession("session"); await flush();
    transport.send(JSON.stringify({type: "resume", approved: true})); await flush();
    expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
    expect(emit).toHaveBeenLastCalledWith({type: "done"});
  });

  it("не переносит запоздавшее подтверждение в другой чат", async () => {
    await waiting();
    let resolve!: (value: Response) => void;
    fetcher.mockReturnValueOnce(new Promise<Response>((r) => { resolve = r; }))
      .mockResolvedValueOnce(response({run: null, legacy: false}));
    transport.send(JSON.stringify({type: "resume", approved: true}));
    await transport.watchSession("new");
    const count = emit.mock.calls.length;
    resolve(response(run)); await flush();
    expect(emit.mock.calls).toHaveLength(count);
  });

  it("восстанавливает задачу без повторной отправки сообщения", async () => {
    fetcher.mockResolvedValueOnce(response({run, legacy: false}))
      .mockResolvedValueOnce(response(run)).mockResolvedValueOnce(page([event(1, "text", "Ответ")]));
    await transport.watchSession("session"); await flush();
    expect(emit).toHaveBeenCalledWith({type: "text", content: "Ответ"});
    expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
    transport.close();
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("повторяет транспортный запрос с тем же request_id", async () => {
    fetcher.mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(response(run)).mockResolvedValueOnce(response(run)).mockResolvedValueOnce(page());
    transport.send(JSON.stringify({type: "message", content: "Тест", session_id: "session"})); await flush();
    const posts = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0][1]?.body).toBe(posts[1][1]?.body);
    expect(JSON.parse(String(posts[0][1]?.body)).request_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("не завершает UI по chat.done до приёмки WorkOrder", async () => {
    fetcher.mockResolvedValueOnce(response({run, legacy: false}))
      .mockResolvedValueOnce(response({...run, status: "verifying"}))
      .mockResolvedValueOnce(page([event(1, "done")]));
    await transport.watchSession("session"); await flush();
    expect(emit).not.toHaveBeenCalledWith({type: "done"});
    expect(emit).toHaveBeenCalledWith({type: "durable_state", active: true});
  });

  it("не дублирует уже загруженный из истории ответ", async () => {
    const completed = {...run, status: "completed", result_message_id: "answer"};
    fetcher.mockResolvedValueOnce(response({run: completed, legacy: false}))
      .mockResolvedValueOnce(response(completed)).mockResolvedValueOnce(page([event(1, "text", "Ответ")]));
    await transport.watchSession("session", new Set(["answer"])); await flush();
    expect(emit).not.toHaveBeenCalledWith({type: "text", content: "Ответ"});
    expect(emit).toHaveBeenCalledWith({type: "done"});
  });

  it("отменяет через WorkOrder, не закрытием соединения", async () => {
    fetcher.mockResolvedValueOnce(response({run, legacy: false}))
      .mockResolvedValueOnce(response(run)).mockResolvedValueOnce(page());
    await transport.watchSession("session"); await flush();
    fetcher.mockResolvedValueOnce(response({status: "canceled"}));
    transport.send(JSON.stringify({type: "stop"})); await flush();
    expect(fetcher).toHaveBeenLastCalledWith("/api/work-orders/order/cancel", expect.objectContaining({method: "POST"}));
    expect(emit).not.toHaveBeenCalledWith({type: "done"});
  });

  it("игнорирует запоздавший ответ от ранее выбранного чата", async () => {
    let resolve!: (value: Response) => void;
    fetcher.mockReturnValueOnce(new Promise<Response>((r) => { resolve = r; }))
      .mockResolvedValueOnce(response({run: null, legacy: false}));
    const old = transport.watchSession("old");
    await transport.watchSession("new");
    resolve(response({run, legacy: true})); await old; await flush();
    expect(emit).not.toHaveBeenCalledWith({type: "durable_mode", legacy: true});
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("передаёт вложения и контекст, не создаёт второй запрос при busy", async () => {
    fetcher.mockResolvedValueOnce(response(run)).mockResolvedValueOnce(response(run)).mockResolvedValueOnce(page());
    const attachments = [{document_id: "doc", file_name: "file.pdf"}];
    const workspace_context = {surface: "table"};
    transport.send(JSON.stringify({type: "message", content: "Тест", session_id: "session", attachments, workspace_context}));
    transport.send(JSON.stringify({type: "message", content: "Повтор"})); await flush();
    const posts = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({attachments, workspace_context});
  });
});
