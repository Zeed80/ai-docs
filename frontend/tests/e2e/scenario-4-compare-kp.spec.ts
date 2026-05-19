/**
 * Scenario 4: Compare КП (Commercial Proposals)
 * Flow: user navigates to /compare, creates session, aligns items, picks supplier.
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

const SESSION_ID = "aaaaaaaa-0000-4000-8000-000000000001";
const INV_1 = "bbbbbbbb-0000-4000-8000-000000000001";
const INV_2 = "bbbbbbbb-0000-4000-8000-000000000002";

const draftSession = {
  id: SESSION_ID,
  name: "Закупка Вал Ø25",
  status: "draft",
  invoice_ids: [INV_1, INV_2],
  alignment: null,
  decision: null,
  decided_by: null,
  decided_at: null,
  created_at: "2026-05-01T10:00:00Z",
};

const alignedSession = {
  ...draftSession,
  status: "aligned",
  alignment: {
    items: [
      {
        canonical_name: "Вал Ø25×200",
        items: {
          [INV_1]: {
            description: "Вал",
            quantity: 10,
            unit: "шт",
            unit_price: 850,
            amount: 8500,
          },
          [INV_2]: {
            description: "Вал Ø25",
            quantity: 10,
            unit: "шт",
            unit_price: 790,
            amount: 7900,
          },
        },
      },
    ],
    suppliers: {
      [INV_1]: {
        name: "ООО Металл-Сервис",
        invoice_number: "МС-001",
        total_amount: 8500,
      },
      [INV_2]: {
        name: "ИП Стальторг",
        invoice_number: "СТ-042",
        total_amount: 7900,
      },
    },
  },
};

const decidedSession = {
  ...alignedSession,
  status: "decided",
  decision: { chosen_supplier_id: INV_2, reasoning: "Лучшая цена на единицу" },
  decided_by: "e2e",
  decided_at: "2026-05-17T12:00:00Z",
};

const summary = {
  total_items: 1,
  suppliers: [
    {
      supplier_id: INV_1,
      name: "ООО Металл-Сервис",
      invoice_number: "МС-001",
      total: 8500,
      invoice_total: 8500,
    },
    {
      supplier_id: INV_2,
      name: "ИП Стальторг",
      invoice_number: "СТ-042",
      total: 7900,
      invoice_total: 7900,
    },
  ],
  cheapest_total: {
    supplier_id: INV_2,
    name: "ИП Стальторг",
    invoice_number: "СТ-042",
    total: 7900,
    invoice_total: 7900,
  },
  recommendation: "Рекомендуем ИП Стальторг — самая низкая цена: 7 900 ₽",
};

type MockSession =
  | typeof draftSession
  | typeof alignedSession
  | typeof decidedSession;

async function mockApi(page: Page) {
  let session: MockSession = { ...draftSession };

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;

    if (url.pathname === "/api/compare" && request.method() === "GET") {
      return route.fulfill({ json: [session] });
    }
    if (
      url.pathname === `/api/compare/${SESSION_ID}` &&
      request.method() === "GET"
    ) {
      return route.fulfill({ json: session });
    }
    if (
      url.pathname === `/api/compare/${SESSION_ID}/summary` &&
      request.method() === "GET"
    ) {
      return route.fulfill({
        json: session.status === "draft" ? null : summary,
      });
    }
    if (
      url.pathname === `/api/compare/${SESSION_ID}/align` &&
      request.method() === "POST"
    ) {
      session = { ...alignedSession };
      return route.fulfill({ json: session });
    }
    if (
      url.pathname === `/api/compare/${SESSION_ID}/decide` &&
      request.method() === "POST"
    ) {
      session = { ...decidedSession };
      return route.fulfill({ json: session });
    }
    if (
      url.pathname === `/api/compare/${SESSION_ID}/draft-rejections` &&
      request.method() === "POST"
    ) {
      return route.fulfill({ json: { draft_ids: ["draft-1"] } });
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
          created_at: "2026-05-17T12:00:00Z",
          updated_at: "2026-05-17T12:00:00Z",
        },
      });
    }
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("Scenario 4: compare КП list shows session", async ({ page, context }) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/compare");

  await expect(page.getByText("Закупка Вал Ø25")).toBeVisible();
  await expect(page.getByText("Черновик")).toBeVisible();
});

test("Scenario 4: align items, pick supplier, mark decided", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/compare/${SESSION_ID}`);

  // Initial state: draft, no alignment
  await expect(page.getByText("Закупка Вал Ø25")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Выровнять позиции" }),
  ).toBeVisible();

  // Align
  await page.getByRole("button", { name: "Выровнять позиции" }).click();

  // After align: see aligned items and "Принять решение" button
  await expect(page.getByText("Вал Ø25×200")).toBeVisible({ timeout: 8000 });
  await expect(page.getByText("ООО Металл-Сервис").first()).toBeVisible();
  await expect(page.getByText("ИП Стальторг").first()).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Принять решение" }),
  ).toBeVisible();
  await expect(page.getByText("Рекомендуем ИП Стальторг")).toBeVisible();

  // Open decide form
  await page.getByRole("button", { name: "Принять решение" }).click();

  // The decide form has a select with supplier options (there may be multiple selects on page)
  const supplierSelect = page.locator("select").first();
  await expect(supplierSelect).toBeVisible();
  await supplierSelect.selectOption({ value: INV_2 });

  // Click decide submit button
  const submitBtn = page.getByRole("button", { name: "Подтвердить" });
  await expect(submitBtn).toBeVisible();
  await submitBtn.click();

  // After decide: see decision badge
  await expect(page.getByText("✓ Выбрано")).toBeVisible({ timeout: 8000 });
  await expect(page.getByText("Лучшая цена на единицу")).toBeVisible();
});
