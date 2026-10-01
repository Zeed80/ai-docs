import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AssistantPanel } from "@/components/chat/assistant-panel";
import { mutFetch } from "@/lib/auth";
import { getChatMessages, importArchivedConversation, listChatSessions } from "@/lib/api";

vi.mock("next/navigation", () => ({useRouter: () => ({push: vi.fn()})}));
vi.mock("@/lib/degraded-mode", () => ({useDegradedMode: () => ({isDegraded: false})}));
vi.mock("@/lib/agent-name", () => ({useAgentName: () => "Света"}));
vi.mock("@/components/gpu-status-bar", () => ({GpuStatusBar: () => null}));
vi.mock("@/lib/native-bridge", () => ({isNative: () => false, speechAvailable: async () => false, scanDocument: vi.fn(), dictate: vi.fn()}));
vi.mock("@/lib/auth", () => ({mutFetch: vi.fn()}));
vi.mock("@/lib/api", () => ({
  listChatSessions: vi.fn(async () => [{id: "session", title: "Новый чат", created_at: "2026-09-11T00:00:00Z"}]),
  getChatMessages: vi.fn(async () => []),
  importArchivedConversation: vi.fn(),
  createChatSession: vi.fn(), deleteChatSession: vi.fn(),
}));
const fetcher = vi.mocked(mutFetch);
const listSessions = vi.mocked(listChatSessions);
const getMessages = vi.mocked(getChatMessages);
const importArchive = vi.mocked(importArchivedConversation);
const run = {id: "run", session_id: "session", work_order_id: "order", status: "running", result_message_id: null};
const response = (value: unknown) => new Response(JSON.stringify(value));

beforeEach(() => {
  window.localStorage.clear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  fetcher.mockReset();
  listSessions.mockReset();
  listSessions.mockResolvedValue([{id: "session", title: "Новый чат", user_key: "dev-user", created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", last_message_at: null}]);
  getMessages.mockReset();
  getMessages.mockResolvedValue([]);
  importArchive.mockReset();
});
afterEach(() => { cleanup(); });

it("основная панель отправляет HTTP-задачу без WebSocket и не отменяет её при закрытии", async () => {
  const socket = vi.spyOn(window, "WebSocket");
  fetcher.mockImplementation(async (path, init) => {
    if (path === "/api/ai/agent-config") return response({});
    if (path.includes("?session_id=")) return response({run: null, legacy: false});
    if (path === "/api/agent/chat-runs" && init?.method === "POST") return response(run);
    if (path.includes("/events?")) return response({items: [], next_cursor: 0});
    return response(run);
  });
  const view = render(<AssistantPanel />);
  const input = await screen.findByRole("textbox", {name: "Сообщение Света"});
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("?session_id=session"), expect.anything()));
  fireEvent.change(input, {target: {value: "Проверь состояние"}});
  fireEvent.keyDown(input, {key: "Enter"});
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/agent/chat-runs", expect.objectContaining({method: "POST"})));
  expect(socket).not.toHaveBeenCalled();
  expect(await screen.findByRole("link", {name: "Журнал и сверка действий"})).toHaveAttribute("href", "/work-orders/chat-journal?run_id=run");
  view.unmount();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith("/cancel"))).toBe(false);
  socket.mockRestore();
});

it("показывает legacy approval request только для чтения", async () => {
  const completed = {...run, status: "completed"};
  fetcher.mockImplementation(async (path) => {
    if (path === "/api/ai/agent-config") return response({});
    if (path.includes("?session_id=")) return response({run: completed, legacy: false});
    if (path.endsWith("/events?after=0&limit=100")) return response({
      items: [{sequence: 1, type: "chat.approval", payload: {event: {
        type: "approval_request", tool: "email.send", preview: "Отправить письмо",
        approval_id: "legacy-approval", db_id: "legacy-db",
      }}}],
      next_cursor: 1,
    });
    return response(completed);
  });

  render(<AssistantPanel />);
  expect(await screen.findByText(/Устаревший запрос: решение не отправлено/)).toBeInTheDocument();
  expect(screen.queryByRole("button", {name: "Утвердить"})).not.toBeInTheDocument();
  expect(screen.queryByRole("button", {name: "Отклонить"})).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
});

it("в архивном чате ввод отключён с объяснением, история не мигрирует молча", async () => {
  fetcher.mockImplementation(async (path) => response(path.includes("?session_id=") ? {run: null, legacy: true} : {}));
  render(<AssistantPanel />);
  await waitFor(() => expect(screen.getByRole("textbox", {name: "Сообщение Света"})).toBeDisabled());
  expect(screen.getByText(/Архивный чат: выберите сообщения/)).toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Продолжить с выбранным контекстом"})).toBeDisabled();
});

it("переносит только явно выбранные архивные сообщения в новую сессию", async () => {
  const source = {id: "session", title: "Archive", user_key: "dev-user", created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", last_message_at: "2026-09-11T00:00:00Z"};
  const target = {...source, id: "target", title: "Продолжение архивного чата"};
  listSessions.mockResolvedValueOnce([source]).mockResolvedValue([target, source]);
  getMessages.mockImplementation(async (sessionId) => sessionId === "session" ? [
    {id: "m-user", session_id: "session", role: "user", content: "old question", metadata: null, created_at: "2026-09-11T00:00:00Z", attachments: [{id: "att-1", message_id: "m-user", document_id: "doc-1", file_name: "old.pdf", mime_type: "application/pdf", size_bytes: 10, created_at: "2026-09-11T00:00:00Z"}]},
    {id: "m-assistant", session_id: "session", role: "assistant", content: "old answer", metadata: {tool_calls: [{id: "must-not-send"}]}, created_at: "2026-09-11T00:00:01Z", attachments: []},
  ] : []);
  importArchive.mockResolvedValue({id: "import-1", source_session_id: "session", target_session_id: "target", record_count: 1, created: true});
  fetcher.mockImplementation(async (path) => response(path.includes("session_id=session") ? {run: null, legacy: true} : {run: null, legacy: false}));

  render(<AssistantPanel />);
  const selected = await screen.findByRole("checkbox", {name: "Выбрать архивное сообщение m-user"});
  fireEvent.click(selected);
  fireEvent.click(screen.getByRole("button", {name: "Продолжить с выбранным контекстом"}));
  await waitFor(() => expect(importArchive).toHaveBeenCalledTimes(1));
  expect(importArchive).toHaveBeenCalledWith("session", expect.objectContaining({
    message_ids: ["m-user"],
    attachment_ids: ["att-1"],
  }));
  expect(JSON.stringify(importArchive.mock.calls[0])).not.toContain("must-not-send");
  await waitFor(() => expect(screen.getByRole("textbox", {name: "Сообщение Света"})).toBeEnabled());
});

it("после неоднозначной ошибки и перезагрузки повторяет тот же archive request_id", async () => {
  getMessages.mockResolvedValue([
    {id: "m-user", session_id: "session", role: "user", content: "old question", metadata: null, created_at: "2026-09-11T00:00:00Z", attachments: []},
  ]);
  importArchive
    .mockRejectedValueOnce(new TypeError("network reply lost"))
    .mockResolvedValueOnce({id: "import-1", source_session_id: "session", target_session_id: "target", record_count: 1, created: false});
  listSessions.mockResolvedValue([
    {id: "session", title: "Archive", user_key: "dev-user", created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", last_message_at: null},
    {id: "target", title: "Continuation", user_key: "dev-user", created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", last_message_at: null},
  ]);
  fetcher.mockImplementation(async (path) => response(path.includes("session_id=session") ? {run: null, legacy: true} : {run: null, legacy: false}));

  const view = render(<AssistantPanel />);
  fireEvent.click(await screen.findByRole("checkbox", {name: "Выбрать архивное сообщение m-user"}));
  const button = screen.getByRole("button", {name: "Продолжить с выбранным контекстом"});
  fireEvent.click(button);
  await waitFor(() => expect(importArchive).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(button).toBeEnabled());
  view.unmount();
  render(<AssistantPanel />);
  fireEvent.click(await screen.findByRole("checkbox", {name: "Выбрать архивное сообщение m-user"}));
  fireEvent.click(screen.getByRole("button", {name: "Продолжить с выбранным контекстом"}));
  await waitFor(() => expect(importArchive).toHaveBeenCalledTimes(2));
  const firstRequest = importArchive.mock.calls[0]?.[1].request_id;
  const secondRequest = importArchive.mock.calls[1]?.[1].request_id;
  expect(secondRequest).toBe(firstRequest);
});

it("не начинает импорт без сохранённого ключа безопасного повтора", async () => {
  getMessages.mockResolvedValue([
    {id: "m-user", session_id: "session", role: "user", content: "old question", metadata: null, created_at: "2026-09-11T00:00:00Z", attachments: []},
  ]);
  fetcher.mockImplementation(async (path) => response(path.includes("?session_id=") ? {run: null, legacy: true} : {}));
  render(<AssistantPanel />);
  fireEvent.click(await screen.findByRole("checkbox", {name: "Выбрать архивное сообщение m-user"}));
  const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("quota", "QuotaExceededError");
  });
  fireEvent.click(screen.getByRole("button", {name: "Продолжить с выбранным контекстом"}));
  await waitFor(() => expect(importArchive).not.toHaveBeenCalled());
  storage.mockRestore();
});
