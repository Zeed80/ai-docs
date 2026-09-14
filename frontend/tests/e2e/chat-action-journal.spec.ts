import { expect, test } from "@playwright/test";

test("ссылка на журнал сохраняется в перенаправлении на вход", async ({request}) => {
  const path = "/work-orders/chat-journal?run_id=11111111-1111-4111-8111-111111111111";
  const response = await request.get(path, {maxRedirects: 0});
  expect(response.status()).toBe(307);
  const location = new URL(response.headers().location);
  expect(location.pathname).toBe("/auth/login");
  expect(location.searchParams.get("next")).toBe(path);
});

test("наблюдение сохраняется после reload, но не возобновляет неизвестное действие", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const runId = "11111111-1111-4111-8111-111111111111";
  const action = {id: "action", tool: "email.send", status: "outcome_unknown", request_digest: "a".repeat(64)};
  let observation: Record<string, unknown> | null = null;
  const writes: string[] = [];
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") writes.push(path);
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [{id: runId, title: "Журнал", created_at: "2026-09-14T00:00:00Z"}]});
    if (path.endsWith("/messages")) return route.fulfill({json: []});
    if (path === "/api/agent/chat-runs") return route.fulfill({json: {run: null, legacy: false}});
    if (path.endsWith("/observations")) {
      observation = {...route.request().postDataJSON(), actor: "test-user", verified: false, can_replay: false};
      return route.fulfill({status: 201, json: observation});
    }
    if (path.endsWith("/actions")) return route.fulfill({json: {items: [{...action, latest_observation: observation}], next_offset: 1, work_order_status: "blocked"}});
    if (path.endsWith("/actions/action")) return route.fulfill({json: {...action, request: {draft_id: "draft-42"}, result: null, result_digest: null}});
    return route.fulfill({json: {}});
  });
  await page.goto(`/work-orders/chat-journal?run_id=${runId}`);
  await page.getByRole("button", {name: /email.send/}).click();
  await expect(page.getByText(/draft-42/)).toBeVisible();
  await page.getByLabel("Наблюдаемый исход").selectOption("not_observed");
  await page.getByLabel("Обоснование").fill("В журнале получателя запись не найдена");
  await page.getByLabel("Ссылка или идентификатор свидетельства").fill("recipient-log:42");
  await page.getByRole("button", {name: "Сохранить наблюдение"}).click();
  await expect(page.getByText("Последнее наблюдение — не проверено")).toBeVisible();
  await page.reload();
  await page.getByRole("button", {name: /email.send/}).click();
  await expect(page.getByText("В журнале получателя запись не найдена")).toBeVisible();
  await expect(page.getByText("Состояние работы: blocked")).toBeVisible();
  expect(writes).toEqual([`/api/agent/chat-runs/${runId}/actions/action/observations`]);
  await expect(page.getByRole("button", {name: /Возобновить|Разрешить действие/})).toHaveCount(0);
});
