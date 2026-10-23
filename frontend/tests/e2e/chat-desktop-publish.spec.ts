/**
 * Публикация из чата открывает Рабочий стол, ссылки в ответе открываются в
 * новой вкладке (владелец, 2026-10-23). Публикация из истории при повторном
 * открытии страницы никуда не переводит.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test chat-desktop-publish --project=chromium
 */

import { expect, test } from "@playwright/test";

test("живая публикация открывает Рабочий стол, история — нет", async ({ context, page }) => {
  await context.addCookies([{ name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/" }]);
  const session = {
    id: "22222222-2222-4222-8222-222222222222", title: "Таблица",
    user_key: "test-user", created_at: "2026-10-23T00:00:00Z", updated_at: "2026-10-23T00:00:00Z",
  };
  let submitted = false;
  let completed = false;
  const run = () => ({
    id: "run", session_id: session.id, work_order_id: "order", request_id: "request",
    status: completed ? "completed" : "running", result_message_id: null,
  });
  const events = [
    { sequence: 1, type: "chat.workspace.updated", payload: { event: { type: "workspace.updated", canvas_id: "agent:t" } } },
    { sequence: 2, type: "chat.text", payload: { event: { type: "text", content: "Готово, каталог: https://example.com/cat.pdf." } } },
  ];
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({ json: { sub: "test-user", name: "Test", email: "test@invalid", roles: ["admin"], groups: [], sections: [] } });
    if (path === "/api/ai/agent-config") return route.fulfill({ json: { agent_name: "Света" } });
    if (path === "/api/chat/sessions") return route.fulfill({ json: [session] });
    if (path.endsWith("/messages")) return route.fulfill({ json: [] });
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") { submitted = true; return route.fulfill({ status: 202, json: run() }); }
      return route.fulfill({ json: { run: submitted ? run() : null, legacy: false } });
    }
    if (path.endsWith("/events")) {
      return route.fulfill({ json: { items: completed ? events : [], next_cursor: completed ? 2 : 0 } });
    }
    if (path === "/api/agent/chat-runs/run") return route.fulfill({ json: run() });
    return route.fulfill({ json: {} });
  });

  await page.goto("/invoices");
  const input = page.getByRole("textbox", { name: "Сообщение Света" }).first();
  await expect(input).toBeEnabled();
  await input.fill("Покажи таблицу");
  await input.press("Enter");
  await expect.poll(() => submitted).toBe(true);
  completed = true;
  await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/);

  const link = page.getByRole("link", { name: "https://example.com/cat.pdf" });
  await expect(link).toHaveAttribute("target", "_blank");

  await page.goto("/invoices");
  await expect(page.getByRole("link", { name: "https://example.com/cat.pdf" })).toBeVisible();
  await expect(page).toHaveURL(/\/invoices$/);
});
