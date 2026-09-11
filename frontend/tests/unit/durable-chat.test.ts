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
