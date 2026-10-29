/**
 * W6: ответ с веб-источниками. Проверенный ответ (answer.revised) заменяет
 * черновик, сноски [n] ведут на прочитанные страницы, карточка поиска
 * раскрывается и показывает страницы и таблицу фактов.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test chat-web-sources --project=chromium
 */

import { expect, test } from "@playwright/test";

const MAKER = "https://hms.example/km-100-80-160";
const SHOP = "https://shop.example/km";

test("сноски ведут на прочитанные страницы, карточка поиска раскрывается", async ({
  context,
  page,
}) => {
  await context.addCookies([
    { name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/" },
  ]);
  const session = {
    id: "33333333-3333-4333-8333-333333333333",
    title: "Насос",
    user_key: "test-user",
    created_at: "2026-10-30T00:00:00Z",
    updated_at: "2026-10-30T00:00:00Z",
  };
  let submitted = false;
  let completed = false;
  const run = () => ({
    id: "run",
    session_id: session.id,
    work_order_id: "order",
    request_id: "request",
    status: completed ? "completed" : "running",
    result_message_id: null,
  });
  const research = {
    version: 1,
    status: "ok",
    data: {
      mode: "deep",
      queries: ["КМ 100-80-160 паспорт"],
      facts: [
        { name: "напор", status: "confirmed", values: [{ value: "32", unit: "м", sources: [1, 2] }] },
        { name: "масса", status: "missing", values: [] },
      ],
      sources: [
        { n: 1, cite_as: "[1]", url: MAKER, title: "Ливгидромаш", kind: "manufacturer", read_status: "ok", trusted: true },
        { n: 2, cite_as: "[2]", url: SHOP, title: "Магазин", kind: "marketplace", read_status: "ok" },
        { n: 3, cite_as: "[3]", url: "https://wall.example/", read_status: "blocked", challenge: "qrator" },
      ],
    },
  };
  const events = [
    { sequence: 1, type: "chat.tool_call", payload: { event: { type: "tool_call", tool: "search", args: { action: "research", mode: "deep" } } } },
    { sequence: 2, type: "chat.tool_result", payload: { event: { type: "tool_result", tool: "search", result: research } } },
    { sequence: 3, type: "chat.text", payload: { event: { type: "text", content: "Напор 32 м [1], масса 165 кг [1]." } } },
    {
      sequence: 4,
      type: "chat.web.sources",
      payload: {
        event: {
          type: "web.sources",
          sources: [{ n: 1, url: MAKER, title: "Ливгидромаш", domain: "hms.example", kind: "manufacturer", read_status: "ok" }],
        },
      },
    },
    {
      sequence: 5,
      type: "chat.answer.revised",
      payload: {
        event: {
          type: "answer.revised",
          content: `Напор 32 м [1]; массу у производителя не нашёл.\n\nИсточники:\n[1] Ливгидромаш — ${MAKER}`,
        },
      },
    },
  ];
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me")
      return route.fulfill({
        json: { sub: "test-user", name: "Test", email: "test@invalid", roles: ["admin"], groups: [], sections: [] },
      });
    if (path === "/api/ai/agent-config") return route.fulfill({ json: { agent_name: "Света" } });
    if (path === "/api/chat/sessions") return route.fulfill({ json: [session] });
    if (path.endsWith("/messages")) return route.fulfill({ json: [] });
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") {
        submitted = true;
        return route.fulfill({ status: 202, json: run() });
      }
      return route.fulfill({ json: { run: submitted ? run() : null, legacy: false } });
    }
    if (path.endsWith("/events"))
      return route.fulfill({
        json: { items: completed ? events : [], next_cursor: completed ? 5 : 0 },
      });
    if (path === "/api/agent/chat-runs/run") return route.fulfill({ json: run() });
    return route.fulfill({ json: {} });
  });

  await page.goto("/invoices");
  const input = page.getByRole("textbox", { name: "Сообщение Света" }).first();
  await expect(input).toBeEnabled();
  await input.fill("Найди характеристики КМ 100-80-160");
  await input.press("Enter");
  await expect.poll(() => submitted).toBe(true);
  completed = true;

  // The checked answer replaced the draft with the unconfirmed 165 kg.
  await expect(page.getByText("массу у производителя не нашёл", { exact: false })).toBeVisible();
  await expect(page.getByText("165 кг")).toHaveCount(0);
  const footnote = page.getByRole("link", { name: "Источник 1: Ливгидромаш" }).first();
  await expect(footnote).toHaveAttribute("href", MAKER);
  await expect(footnote).toHaveAttribute("target", "_blank");

  // The search card opens to the pages and the facts.
  await page.getByText("источники ▾").click();
  await expect(page.getByText("подтверждено", { exact: true })).toBeVisible();
  await expect(page.getByText("не найдено", { exact: true })).toBeVisible();
  await expect(page.getByText("в реестре")).toBeVisible();
  await expect(page.getByText("проверка на бота (qrator)")).toBeVisible();
});
