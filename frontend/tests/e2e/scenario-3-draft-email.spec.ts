/**
 * Scenario 3: Draft Email
 * Flow: user opens email page, composes a draft to a supplier,
 * saves the draft, sees it in the Черновики list.
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

const DRAFT_ID = "dddddddd-0001-4000-8000-000000000001";

const savedDraft = {
  id: DRAFT_ID,
  to_address: "supplier@acme.ru",
  subject: "Запрос КП на болты М8",
  body: "Уважаемые коллеги, просим предоставить КП.",
  status: "draft",
  risk_score: null,
  risk_flags: [],
};

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/email/threads" && request.method() === "GET")
      return route.fulfill({
        json: { items: [], total: 0 },
      });
    if (url.pathname === "/api/email/drafts" && request.method() === "GET")
      return route.fulfill({ json: [savedDraft] });
    if (url.pathname === "/api/email/drafts" && request.method() === "POST")
      return route.fulfill({ status: 201, json: savedDraft });

    // Shared stubs
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
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET")
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/chat/sessions" && request.method() === "POST")
      return route.fulfill({
        json: {
          id: "chat-e2e",
          title: "Новый чат",
          created_at: "2026-05-19T12:00:00Z",
          updated_at: "2026-05-19T12:00:00Z",
        },
      });
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("Scenario 3: email page shows inbox and draft tabs", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/email");

  await expect(page.getByRole("button", { name: "Входящие" })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByRole("button", { name: /Черновики/ })).toBeVisible({
    timeout: 5_000,
  });
});

test("Scenario 3: clicking Черновики tab shows draft list", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/email");

  await expect(page.getByRole("button", { name: /Черновики/ })).toBeVisible({
    timeout: 10_000,
  });
  await page.getByRole("button", { name: /Черновики/ }).click();

  // Draft from mock should appear
  await expect(page.getByText("Запрос КП на болты М8")).toBeAttached({
    timeout: 5_000,
  });
});

test("Scenario 3: compose button opens draft form with required fields", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/email");

  // Wait for page to load
  await expect(page.getByRole("button", { name: /Черновики/ })).toBeVisible({
    timeout: 10_000,
  });

  // Find and click "Compose" / "Написать" / "N" keyboard shortcut button
  // The email page uses keyboard shortcut 'n' to compose, or has a button
  const composeBtn = page.getByRole("button", {
    name: /написать|compose|новое письмо/i,
  });
  if ((await composeBtn.count()) > 0) {
    await composeBtn.first().click();
  } else {
    // Trigger compose via keyboard shortcut 'n'
    await page.keyboard.press("n");
  }

  // Compose form should show to_address and subject fields
  const toField = page.locator(
    'input[placeholder*="кому"], input[placeholder*="адрес"], input[type="email"], input[name="to_address"]',
  );
  await expect(toField.first()).toBeAttached({ timeout: 5_000 });
});
