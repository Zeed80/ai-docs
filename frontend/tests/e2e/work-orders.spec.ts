import { expect, test } from "@playwright/test";

test("operator creates and inspects a durable work order", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "access_token",
      value: "e2e-token",
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  const id = "11111111-1111-4111-8111-111111111111";
  let orders: Array<Record<string, unknown>> = [];
  await page.route("**/api/approvals/pending**", async (route) => {
    return route.fulfill({ json: { items: [], total: 0 } });
  });
  await page.route("**/api/work-orders**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/metrics"))
      return route.fulfill({
        json: { window_hours: 24, status_counts: {}, step_durations: {} },
      });
    if (url.pathname.endsWith("/plan"))
      return route.fulfill({
        json: {
          steps: [
            {
              id: "s1",
              step_key: "lookup",
              title: "Найти документы",
              kind: "capability",
              capability: "documents",
              action: "search",
              depends_on: [],
              state: "ready",
              attempt_count: 0,
              max_attempts: 3,
            },
          ],
        },
      });
    if (url.pathname.endsWith("/events"))
      return route.fulfill({
        json: [
          {
            sequence: 1,
            event_type: "work.created",
            actor: "e2e",
            payload: {},
            created_at: new Date().toISOString(),
          },
        ],
      });
    if (url.pathname.endsWith("/learning"))
      return route.fulfill({
        json: {
          status: "recorded",
          summary: "Проверенный результат сохранён",
          lessons: [{ kind: "verified_outcome" }],
          provenance: { work_order_id: id },
          memory_fact_id: "memory-1",
          recipe_skill_id: null,
          extraction_attempts: 1,
        },
      });
    if (url.pathname.endsWith("/tool-calls"))
      return route.fulfill({ json: [] });
    if (request.method() === "POST" && url.pathname === "/api/work-orders") {
      const payload = request.postDataJSON();
      const row = {
        id,
        objective: payload.objective,
        description: payload.description,
        status: "planning",
        priority: 50,
        risk_level: "low",
        plan_revision: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      orders = [row];
      return route.fulfill({ status: 201, json: row });
    }
    if (request.method() === "GET" && url.pathname === "/api/work-orders")
      return route.fulfill({ json: orders });
    return route.fulfill({ json: {} });
  });

  await page.goto("/work-orders");
  await page
    .getByPlaceholder("Что нужно сделать?")
    .fill("Собрать отчёт по документам");
  await page.getByRole("button", { name: "Поставить поручение" }).click();
  await expect(
    page.getByRole("heading", { name: "Собрать отчёт по документам" }),
  ).toBeVisible();
  await expect(page.getByText("documents.search")).toBeVisible();
  await expect(page.getByText("work.created")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Память исполнения" }),
  ).toBeVisible();
  await expect(page.getByText("Проверенный результат сохранён")).toBeVisible();
  // Б13.1: manual step-run is a labelled debug override, not the primary action.
  await expect(
    page.getByRole("button", { name: "Форсировать шаг вручную (debug)" }),
  ).toBeVisible();
});

test("operator sees tool-call evidence and approves a gated step inline", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "access_token",
      value: "e2e-token",
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  const id = "22222222-2222-4222-8222-222222222222";
  const approvalId = "33333333-3333-4333-8333-333333333333";
  const decideRequests: Array<Record<string, unknown>> = [];

  await page.route("**/api/approvals/pending**", async (route) => {
    return route.fulfill({
      json: {
        items: [
          {
            id: approvalId,
            status: "pending",
            context: {
              work_order_id: id,
              step_id: "s1",
              tool_name: "documents",
              action: "bulk_delete",
              tool_args: { ids: [1, 2] },
            },
          },
        ],
        total: 1,
      },
    });
  });
  await page.route(`**/api/approvals/${approvalId}/decide`, async (route) => {
    decideRequests.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: approvalId, status: "approved" } });
  });
  await page.route("**/api/work-orders**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/metrics"))
      return route.fulfill({
        json: { window_hours: 24, status_counts: {}, step_durations: {} },
      });
    if (url.pathname.endsWith("/plan"))
      return route.fulfill({
        json: {
          steps: [
            {
              id: "s1",
              step_key: "purge",
              title: "Удалить дубликаты",
              kind: "capability",
              capability: "documents",
              action: "bulk_delete",
              depends_on: [],
              state: "waiting_approval",
              attempt_count: 0,
              max_attempts: 3,
            },
          ],
        },
      });
    if (url.pathname.endsWith("/events")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/learning"))
      return route.fulfill({ json: null });
    if (url.pathname.endsWith("/tool-calls"))
      return route.fulfill({
        json: [
          {
            id: "call-1",
            step_id: "s1",
            executor: "capability",
            capability: "documents",
            action: "bulk_delete",
            arguments: { ids: [1, 2] },
            risk_level: "high",
            status: "waiting_approval",
            action_digest: "abcdef1234567890",
            output: null,
            error: null,
          },
        ],
      });
    if (url.pathname === "/api/work-orders")
      return route.fulfill({
        json: [
          {
            id,
            objective: "Удалить дубликаты документов",
            description: "",
            status: "waiting_approval",
            priority: 50,
            risk_level: "high",
            plan_revision: 1,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ],
      });
    return route.fulfill({ json: {} });
  });

  await page.goto("/work-orders");
  await page.getByText("Удалить дубликаты документов").click();
  await expect(
    page.getByRole("heading", { name: "Удалить дубликаты документов" }),
  ).toBeVisible();

  // Б13.3: the pending approval renders inline on its exact step.
  await expect(
    page.getByText("documents.bulk_delete", { exact: true }),
  ).toBeVisible();
  const approveButton = page.getByRole("button", { name: "Одобрить" });
  await expect(approveButton).toBeVisible();

  // Б13.2: tool-call evidence is visible and expandable. The pending-approval
  // preview above already shows the same {"ids": [1, 2]} args, so assert by
  // count (1 -> 2) rather than a text locator that would match both.
  await expect(page.getByText(/digest abcdef1234/)).toBeVisible();
  const idsPreviews = page.getByText('"ids"');
  await expect(idsPreviews).toHaveCount(1);
  await page.getByText(/digest abcdef1234/).click();
  await expect(idsPreviews).toHaveCount(2);

  await approveButton.click();
  await expect.poll(() => decideRequests.length).toBeGreaterThan(0);
  expect(decideRequests[0]).toEqual({ status: "approved" });
});
