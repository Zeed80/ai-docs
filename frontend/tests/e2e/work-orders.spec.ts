import { expect, test } from "@playwright/test";

test("operator creates and inspects a durable work order", async ({ context, page }) => {
  await context.addCookies([{ name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" }]);
  const id = "11111111-1111-4111-8111-111111111111";
  let orders: Array<Record<string, unknown>> = [];
  await page.route("**/api/work-orders**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/plan")) return route.fulfill({ json: { steps: [{ id: "s1", step_key: "lookup", title: "Найти документы", kind: "capability", capability: "documents", action: "search", depends_on: [], state: "ready", attempt_count: 0, max_attempts: 3 }] } });
    if (url.pathname.endsWith("/events")) return route.fulfill({ json: [{ sequence: 1, event_type: "work.created", actor: "e2e", payload: {}, created_at: new Date().toISOString() }] });
    if (request.method() === "POST" && url.pathname === "/api/work-orders") {
      const payload = request.postDataJSON();
      const row = { id, objective: payload.objective, description: payload.description, status: "planning", priority: 50, risk_level: "low", plan_revision: 0, created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
      orders = [row]; return route.fulfill({ status: 201, json: row });
    }
    if (request.method() === "GET" && url.pathname === "/api/work-orders") return route.fulfill({ json: orders });
    return route.fulfill({ json: {} });
  });

  await page.goto("/work-orders");
  await page.getByPlaceholder("Что нужно сделать?").fill("Собрать отчёт по документам");
  await page.getByRole("button", { name: "Поставить поручение" }).click();
  await expect(page.getByRole("heading", { name: "Собрать отчёт по документам" })).toBeVisible();
  await expect(page.getByText("documents.search")).toBeVisible();
  await expect(page.getByText("work.created")).toBeVisible();
});
