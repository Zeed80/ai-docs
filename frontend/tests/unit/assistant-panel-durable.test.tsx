import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AssistantPanel } from "@/components/chat/assistant-panel";
import { mutFetch } from "@/lib/auth";

vi.mock("next/navigation", () => ({useRouter: () => ({push: vi.fn()})}));
vi.mock("@/lib/degraded-mode", () => ({useDegradedMode: () => ({isDegraded: false})}));
vi.mock("@/lib/agent-name", () => ({useAgentName: () => "Света"}));
vi.mock("@/components/gpu-status-bar", () => ({GpuStatusBar: () => null}));
vi.mock("@/lib/native-bridge", () => ({isNative: () => false, speechAvailable: async () => false, scanDocument: vi.fn(), dictate: vi.fn()}));
vi.mock("@/lib/auth", () => ({mutFetch: vi.fn()}));
vi.mock("@/lib/api", () => ({
  listChatSessions: async () => [{id: "session", title: "Новый чат", created_at: "2026-09-11T00:00:00Z"}],
  getChatMessages: async () => [], createChatSession: vi.fn(), deleteChatSession: vi.fn(),
}));
const fetcher = vi.mocked(mutFetch);
const run = {id: "run", session_id: "session", work_order_id: "order", status: "running", result_message_id: null};
const response = (value: unknown) => new Response(JSON.stringify(value));

beforeEach(() => {
  window.localStorage.clear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  fetcher.mockReset();
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
  view.unmount();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith("/cancel"))).toBe(false);
  socket.mockRestore();
});

it("в архивном чате ввод отключён с объяснением, история не мигрирует молча", async () => {
  fetcher.mockImplementation(async (path) => response(path.includes("?session_id=") ? {run: null, legacy: true} : {}));
  render(<AssistantPanel />);
  await waitFor(() => expect(screen.getByRole("textbox", {name: "Сообщение Света"})).toBeDisabled());
  expect(screen.getByText("Архивный чат: создайте новый для долговечного исполнения.")).toBeInTheDocument();
});
