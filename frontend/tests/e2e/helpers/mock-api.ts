/**
 * Общий мок пустого API и cookie авторизации для e2e без бэкенда.
 *
 * Жил внутри section-smoke.spec.ts; вынесен, когда понадобился второму файлу —
 * проверке мобильной раскладки. Копия разошлась бы: разделы добавляют в один
 * список, а второй тест молча продолжал бы ходить в непокрытые эндпоинты.
 */
import type { BrowserContext, Page, Route } from "@playwright/test";

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

export async function setAuthCookie(context: BrowserContext) {
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
export async function mockEmptyApi(page: Page) {
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
    // Часть эндпоинтов отдаёт голый массив, и объект вместо него роняет
    // рендер («sessions.map is not a function») уже после того, как заголовок
    // отрисован, — то есть незаметно для беглой проверки.
    if (
      path.startsWith("/api/chat/sessions") ||
      ARRAY_ENDPOINTS.some((p) => path === p || path.startsWith(`${p}/`))
    ) {
      return route.fulfill({ json: [] });
    }
    // Списочные ответы приходят в двух формах — отдаём обе разом, чтобы не
    // угадывать по каждому разделу.
    return route.fulfill({
      json: { items: [], total: 0, slots: [], instances: [] },
    });
  });
}
