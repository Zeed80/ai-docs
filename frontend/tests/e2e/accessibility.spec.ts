/**
 * Accessibility tests using axe-core.
 * Tests key pages for WCAG 2.1 AA violations.
 */
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([
    {
      name: "access_token",
      value: "e2e-token",
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
}

async function mockSharedApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/auth/me") {
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E Тест",
          preferred_username: "e2e",
          roles: [],
          groups: [],
        },
      });
    }
    if (url.pathname === "/api/dashboard/feed")
      return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/ai/agent-config")
      return route.fulfill({ json: {} });
    if (url.pathname === "/api/workspace/blocks")
      return route.fulfill({ json: { blocks: [], total: 0 } });
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET")
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/chat/sessions" && request.method() === "POST") {
      return route.fulfill({
        json: {
          id: "chat-e2e",
          title: "Новый чат",
          created_at: "2026-05-19T12:00:00Z",
          updated_at: "2026-05-19T12:00:00Z",
        },
      });
    }
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

function checkResults(
  violations: import("axe-core").Result[],
  pageName: string,
) {
  const serious = violations.filter(
    (v) => v.impact === "critical" || v.impact === "serious",
  );
  if (serious.length > 0) {
    const summary = serious
      .map(
        (v) =>
          `[${v.impact}] ${v.id}: ${v.description} (${v.nodes.length} node(s))`,
      )
      .join("\n");
    expect(
      serious,
      `${pageName} has serious accessibility violations:\n${summary}`,
    ).toHaveLength(0);
  }
}

test("Accessibility: main dashboard has no critical violations", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockSharedApi(page);

  await page.goto("/");
  await page.waitForTimeout(1500);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .exclude(".sentry-error-embed")
    .analyze();

  checkResults(results.violations, "Main dashboard");
});

test("Accessibility: anomalies list page has no critical violations", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/anomalies")
      return route.fulfill({
        json: [
          {
            id: "aaa-1",
            anomaly_type: "price_spike",
            severity: "warning",
            status: "open",
            entity_type: "invoice",
            entity_id: "inv-1",
            title: "Скачок цены",
            description: null,
            resolved_by: null,
            resolved_at: null,
            created_at: "2026-05-19T10:00:00Z",
          },
        ],
      });
    if (url.pathname === "/api/auth/me")
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E",
          preferred_username: "e2e",
          roles: [],
          groups: [],
        },
      });
    if (url.pathname === "/api/dashboard/feed")
      return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/ai/agent-config")
      return route.fulfill({ json: {} });
    if (url.pathname.startsWith("/api/chat/sessions"))
      return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto("/anomalies");
  await page.waitForTimeout(1500);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();

  checkResults(results.violations, "Anomalies list");
});

test("Accessibility: compare КП list has no critical violations", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/compare") return route.fulfill({ json: [] });
    if (url.pathname === "/api/auth/me")
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E",
          preferred_username: "e2e",
          roles: [],
          groups: [],
        },
      });
    if (url.pathname === "/api/dashboard/feed")
      return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/ai/agent-config")
      return route.fulfill({ json: {} });
    if (url.pathname.startsWith("/api/chat/sessions"))
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/invoices") return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto("/compare");
  await page.waitForTimeout(1500);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();

  checkResults(results.violations, "Compare КП list");
});
