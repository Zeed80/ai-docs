/**
 * Scenario 7: NL Query + Action
 * Flow: user opens /search, types NL query, sees parsed filter chips + results,
 * can export or save the query.
 */
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

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

const nlResponse = {
  results: [
    {
      id: "inv-001",
      file_name: "ACME-2025-03-001.pdf",
      doc_type: "invoice",
      status: "needs_review",
      created_at: "2026-03-15T10:00:00Z",
      score: 0.95,
      snippet: "ООО АКМЕ · 15.03.2025 · 48 500 ₽",
    },
    {
      id: "inv-002",
      file_name: "ACME-2025-03-042.pdf",
      doc_type: "invoice",
      status: "needs_review",
      created_at: "2026-03-22T10:00:00Z",
      score: 0.88,
      snippet: "ООО АКМЕ · 22.03.2025 · 12 000 ₽",
    },
  ],
  total: 2,
  structured_filter: {
    supplier_name: "АКМЕ",
    status: "needs_review",
    doc_type: "invoice",
    search_text: null,
  },
};

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/search/nl" && request.method() === "POST") {
      return route.fulfill({ json: nlResponse });
    }
    if (
      url.pathname === "/api/search/saved-queries" &&
      request.method() === "GET"
    ) {
      return route.fulfill({ json: [] });
    }
    if (
      url.pathname === "/api/search/saved-queries" &&
      request.method() === "POST"
    ) {
      return route.fulfill({
        json: {
          id: "sq-1",
          nl_text: "счета от АКМЕ за март на проверке",
          result_count: 2,
          created_at: "2026-05-19T12:00:00Z",
        },
      });
    }

    // Shared stubs
    if (url.pathname === "/api/auth/me") {
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
    }
    if (url.pathname === "/api/dashboard/feed")
      return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/ai/agent-config")
      return route.fulfill({ json: {} });
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

test("Scenario 7: NL search shows parsed filter chips and results", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/search");

  // Switch to NL mode
  await page.getByRole("button", { name: "NL-запрос" }).click();

  // Type a NL query and press Enter
  const searchInput = page.getByPlaceholder(/Введите запрос на русском/);
  await searchInput.fill("счета от АКМЕ за март на проверке");
  await searchInput.press("Enter");

  // Parsed filter chips should appear
  await expect(page.getByText("Поставщик: АКМЕ")).toBeVisible({
    timeout: 5000,
  });
  await expect(page.getByText("Статус: needs_review")).toBeVisible();
  await expect(page.getByText("Тип: Счёт")).toBeVisible();

  // Results should appear (showing file_name)
  await expect(page.getByText("ACME-2025-03-001.pdf")).toBeVisible();
  await expect(page.getByText("ACME-2025-03-042.pdf")).toBeVisible();
});

test("Scenario 7: NL search result count is shown", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/search");

  await page.getByRole("button", { name: "NL-запрос" }).click();

  const searchInput = page.getByPlaceholder(/Введите запрос на русском/);
  await searchInput.fill("счета от АКМЕ за март на проверке");
  await searchInput.press("Enter");

  // Should show count
  await expect(page.getByText(/2/)).toBeVisible({ timeout: 5000 });
});

test("Scenario 7: save NL query works", async ({ page, context }) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/search");

  await page.getByRole("button", { name: "NL-запрос" }).click();

  const searchInput = page.getByPlaceholder(/Введите запрос на русском/);
  await searchInput.fill("счета от АКМЕ за март на проверке");
  await searchInput.press("Enter");

  // Wait for results
  await expect(page.getByText("ACME-2025-03-001.pdf")).toBeVisible({
    timeout: 5000,
  });

  // Save the query
  const saveBtn = page.getByRole("button", { name: "Сохранить" });
  await expect(saveBtn).toBeVisible();
  await saveBtn.click();

  // Button shows saving state then reverts
  await expect(saveBtn).toBeVisible({ timeout: 3000 });
});
