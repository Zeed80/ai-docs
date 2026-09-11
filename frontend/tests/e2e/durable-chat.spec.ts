import { expect, test } from "@playwright/test";

test("HTTP-чат восстанавливает работу после reload без повторного POST", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const session = {id: "11111111-1111-4111-8111-111111111111", title: "Проверка восстановления",
    user_key: "test-user", created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z"};
  let submitted = false;
  let completed = false;
  let posts = 0;
  const run = () => ({id: "run", session_id: session.id, work_order_id: "order", request_id: "request",
    status: completed ? "completed" : "running", result_message_id: completed ? "answer" : null});
  const sockets: string[] = [];
  page.on("websocket", (socket) => sockets.push(socket.url()));
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", email: "test@invalid", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/ai/agent-config") return route.fulfill({json: {agent_name: "Света"}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [session]});
    if (path.endsWith("/messages")) return route.fulfill({json: submitted ? [{id: "user-message", role: "user", content: "Проверь задачу", attachments: []}] : []});
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") { posts++; submitted = true; return route.fulfill({status: 202, json: run()}); }
      return route.fulfill({json: {run: submitted ? run() : null, legacy: false}});
    }
    if (path.endsWith("/events")) return route.fulfill({json: {items: completed ? [{sequence: 1, type: "chat.text", payload: {event: {type: "text", content: "Проверка завершена"}}}] : [], next_cursor: completed ? 1 : 0}});
    if (path === "/api/agent/chat-runs/run") return route.fulfill({json: run()});
    return route.fulfill({json: {}});
  });
  await page.goto("/assistant");
  const input = page.getByRole("textbox", {name: "Сообщение Света"}).first();
  await expect(input).toBeEnabled();
  await input.fill("Проверь задачу");
  await input.press("Enter");
  await expect.poll(() => posts).toBe(1);
  await page.reload();
  await expect(page.getByRole("textbox", {name: "Сообщение Света"}).first()).toBeDisabled();
  completed = true;
  await expect(page.getByText("Проверка завершена", {exact: true}).first()).toBeVisible();
  await expect(page.getByRole("textbox", {name: "Сообщение Света"}).first()).toBeEnabled();
  expect(posts).toBe(1);
  expect(sockets.some((url) => url.includes("/ws/chat"))).toBe(false);
});
