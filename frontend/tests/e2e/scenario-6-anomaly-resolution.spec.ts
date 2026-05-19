/**
 * Scenario 6: Anomaly Resolution
 * Flow: user opens AnomalyCard detail, clicks "🤖 Объяснить",
 * sees AI explanation + suggested actions, then resolves the anomaly.
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

const ANOMALY_ID = "ffffffff-0000-4000-8000-000000000001";
const INV_ID = "dddddddd-0000-4000-8000-000000000002";

const openAnomaly = {
  id: ANOMALY_ID,
  anomaly_type: "price_spike",
  severity: "warning",
  status: "open",
  entity_type: "invoice",
  entity_id: INV_ID,
  title: "Скачок цены: Вал Ø25 (+42%)",
  description:
    "Цена позиции выросла с 850 ₽ до 1 207 ₽ (+42%) по сравнению со средним за 90 дней.",
  resolved_by: null,
  resolved_at: null,
  created_at: "2026-05-17T10:00:00Z",
};

const explainResponse = {
  explanation:
    "Обнаружен скачок цены на позицию «Вал Ø25×200» на 42%. " +
    "За последние 90 дней средняя цена составляла 850 ₽/шт. " +
    "Текущая цена от ООО Металл-Сервис: 1 207 ₽/шт. " +
    "Это превышает допустимое отклонение (±15%). " +
    "Возможные причины: смена поставщика-производителя, рост стоимости сырья, ошибка в спецификации.",
  suggested_actions: [
    "Запросить у поставщика обоснование повышения цены",
    "Сравнить с альтернативными предложениями (перейти в раздел «Сравнение КП»)",
    "Одобрить с примечанием, если повышение обосновано",
    "Отклонить счёт и запросить перевыставление",
  ],
};

const resolvedAnomaly = {
  ...openAnomaly,
  status: "resolved",
  resolved_by: "e2e",
  resolved_at: "2026-05-17T12:30:00Z",
};

async function mockApi(page: Page) {
  let anomaly = { ...openAnomaly };

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (
      url.pathname === `/api/anomalies/${ANOMALY_ID}` &&
      request.method() === "GET"
    ) {
      return route.fulfill({ json: anomaly });
    }
    if (
      url.pathname === `/api/anomalies/${ANOMALY_ID}/explain` &&
      request.method() === "GET"
    ) {
      return route.fulfill({ json: explainResponse });
    }
    if (
      url.pathname === `/api/anomalies/${ANOMALY_ID}/resolve` &&
      request.method() === "POST"
    ) {
      anomaly = { ...resolvedAnomaly };
      return route.fulfill({ json: anomaly });
    }
    if (url.pathname === "/api/anomalies" && request.method() === "GET") {
      return route.fulfill({ json: [anomaly] });
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

test("Scenario 6: anomaly detail shows title and description", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/anomalies/${ANOMALY_ID}`);

  await expect(page.getByText("Скачок цены: Вал Ø25 (+42%)")).toBeVisible();
  await expect(page.getByText(/цена позиции выросла/i)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "🤖 Объяснить" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Решить" })).toBeVisible();
});

test("Scenario 6: clicking Объяснить shows AI explanation and actions", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/anomalies/${ANOMALY_ID}`);

  await expect(
    page.getByRole("button", { name: "🤖 Объяснить" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "🤖 Объяснить" }).click();

  // Explanation text appears
  await expect(page.getByText(/обнаружен скачок цены/i)).toBeVisible({
    timeout: 8000,
  });
  await expect(page.getByText("Рекомендуемые действия:")).toBeVisible();

  // Suggested actions rendered
  await expect(
    page.getByText("Запросить у поставщика обоснование повышения цены"),
  ).toBeVisible();
  await expect(
    page.getByText(/Сравнить с альтернативными предложениями/),
  ).toBeVisible();
  await expect(
    page.getByText("Отклонить счёт и запросить перевыставление"),
  ).toBeVisible();
});

test("Scenario 6: resolve anomaly redirects to anomalies list", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/anomalies/${ANOMALY_ID}`);

  await expect(page.getByRole("button", { name: "Решить" })).toBeVisible();
  await page.getByRole("button", { name: "Решить" }).click();

  // After resolve, page navigates to /anomalies
  await expect(page).toHaveURL(/\/anomalies$/, { timeout: 8000 });
});

test("Scenario 6: mark as false positive also navigates away", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/anomalies/${ANOMALY_ID}`);

  await expect(page.getByRole("button", { name: "Ложная" })).toBeVisible();
  await page.getByRole("button", { name: "Ложная" }).click();

  await expect(page).toHaveURL(/\/anomalies$/, { timeout: 8000 });
});
