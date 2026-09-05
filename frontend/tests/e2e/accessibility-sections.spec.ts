/**
 * Доступность списковых разделов.
 *
 * axe покрывал три страницы из восьмидесяти восьми. Здесь те же десять
 * разделов, что и в section-smoke: у списковых экранов свои типовые
 * нарушения — иконочная кнопка без имени, поле без подписи, заголовок с
 * пропущенным уровнем, — и ловить их поштучно глазами бессмысленно.
 *
 * Мок отдаёт пустые ответы: проверяется каркас раздела, а не разметка строк
 * с данными. Пустое состояние — то, что человек видит первым.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test accessibility-sections
 */
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const ARRAY_ENDPOINTS = [
  "/api/anomalies",
  "/api/compare",
  "/api/invoices",
  "/api/collections",
  "/api/canonical",
  "/api/suppliers",
  "/api/boms",
  "/api/drawings",
  "/api/payments",
  "/api/procurement",
  "/api/catalogs",
  "/api/providers/models",
  "/api/providers/instances",
];

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([
    { name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" },
  ]);
}

async function mockEmptyApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") {
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E Тест",
          preferred_username: "e2e",
          roles: ["admin"],
          groups: [],
        },
      });
    }
    if (path.endsWith("/count") || path.endsWith("/unread-count")) {
      return route.fulfill({ json: { count: 0 } });
    }
    if (path === "/api/dashboard/feed") {
      return route.fulfill({ json: { total: 0, items: [] } });
    }
    // Часть эндпоинтов отдаёт голый массив, и объект вместо него роняет
    // рендер («sessions.map is not a function») уже после того, как заголовок
    // отрисован, — то есть незаметно для беглой проверки.
    if (
      path.startsWith("/api/chat/sessions") ||
      ARRAY_ENDPOINTS.some((p) => path === p || path.startsWith(`${p}/`))
    ) {
      return route.fulfill({ json: [] });
    }
    return route.fulfill({
      json: { items: [], total: 0, slots: [], instances: [] },
    });
  });
}

const SECTIONS = [
  "/suppliers",
  "/warehouse",
  "/catalogs",
  "/drawings",
  "/procurement",
  "/payments",
  "/collections",
  "/canonical",
  "/boms",
  "/admin",
  "/settings/models",
];

for (const path of SECTIONS) {
  test(`доступность раздела ${path}`, async ({ page, context }) => {
    await setAuthCookie(context);
    await mockEmptyApi(page);

    await page.goto(path);
    await page.waitForTimeout(1200);

    const { violations } = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .exclude(".sentry-error-embed")
      .analyze();

    const serious = violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious",
    );
    const summary = serious
      .map(
        (v) =>
          `[${v.impact}] ${v.id}: ${v.help}\n    ` +
          v.nodes
            .slice(0, 4)
            .map((n) => n.html.slice(0, 120))
            .join("\n    "),
      )
      .join("\n");
    expect(serious, `${path} — нарушения доступности:\n${summary}`).toEqual([]);
  });
}
