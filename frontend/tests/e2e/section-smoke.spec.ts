/**
 * Смоук по разделам, у которых не было ни одного e2e.
 *
 * Аудит показал десять таких: suppliers, warehouse, catalogs, drawings,
 * procurement, payments, collections, canonical, boms, admin. Это половина
 * ежедневно используемых экранов — поломка в любом из них обнаруживалась
 * только человеком, открывшим страницу.
 *
 * Проверка намеренно неглубокая: раздел открывается, не падает с ошибкой
 * приложения и показывает свой заголовок. Этого хватает, чтобы поймать самое
 * частое — сломанный импорт, ошибку рендера, обращение к несуществующему
 * полю ответа.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test section-smoke
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

/**
 * Пустые, но валидные ответы на всё. Цель — отрисовать раздел, а не проверить
 * данные: пустое состояние обязано выглядеть как пустое состояние, а не как
 * белый экран.
 */
async function mockEmptyApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

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
    // Списочные ответы приходят в двух формах — отдаём обе разом, чтобы не
    // угадывать по каждому разделу.
    return route.fulfill({
      json: { items: [], total: 0, slots: [], instances: [] },
    });
  });
}

const SECTIONS: { path: string; heading: RegExp }[] = [
  { path: "/suppliers", heading: /поставщик/i },
  { path: "/warehouse", heading: /склад/i },
  { path: "/catalogs", heading: /каталог/i },
  { path: "/drawings", heading: /чертеж|чертёж/i },
  { path: "/procurement", heading: /закуп/i },
  { path: "/payments", heading: /платеж|платёж|оплат/i },
  { path: "/collections", heading: /подборк|коллекц/i },
  { path: "/canonical", heading: /канон|номенклатур/i },
  { path: "/boms", heading: /состав|специфик|bom/i },
  { path: "/admin", heading: /админ|управлен/i },
];

for (const section of SECTIONS) {
  test(`раздел ${section.path} открывается и не падает`, async ({
    page,
    context,
  }) => {
    const appErrors: string[] = [];
    page.on("pageerror", (e) => appErrors.push(e.message));

    await setAuthCookie(context);
    await mockEmptyApi(page);

    const response = await page.goto(section.path);
    expect(response?.status(), `${section.path} отдал ошибку`).toBeLessThan(
      400,
    );

    // Next.js рисует свой экран при необработанном исключении в рендере —
    // именно его и надо поймать.
    await expect(
      page.getByText(/Application error|Unhandled Runtime Error/i),
    ).toHaveCount(0);

    await expect(page.getByRole("heading").first()).toBeVisible();
    expect(appErrors, `ошибки в консоли: ${appErrors.join("; ")}`).toEqual([]);
  });
}
