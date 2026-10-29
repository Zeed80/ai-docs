/**
 * «Настройки → Браузер» (W2): профили видны и очищаются после подтверждения,
 * прокси сохраняются без повторной отправки сохранённого пароля,
 * самопроверка показывает, что видят сайты.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test browser-identity --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const SETTINGS = {
  proxies: [
    {
      id: "res-msk",
      server: "http://gw.example:8000",
      username: "u1",
      password_mask: "s3…et",
      password_set: true,
      kind: "residential",
      country: "RU",
      locale: null,
      timezone: null,
      enabled: true,
    },
  ],
  browser_locale: "ru-RU",
  browser_timezone: "Europe/Moscow",
};

async function setup(page: Page, log: Record<string, unknown>[]) {
  await mockEmptyApi(page);
  await page.route("**/api/browser-secrets**", (route: Route) =>
    route.fulfill({ json: [] }),
  );
  let profiles = [
    {
      name: "reader",
      size_bytes: 3 * 1024 * 1024,
      created: 1761600000,
      last_used: 1761690000,
      proxy_id: "res-msk",
      open: true,
      in_use: false,
    },
  ];
  await page.route(
    "**/api/web-search/browser/profiles**",
    async (route: Route) => {
      if (route.request().method() === "DELETE") {
        log.push({
          deleted: new URL(route.request().url()).pathname.split("/").at(-1),
        });
        profiles = [];
        return route.fulfill({ json: { ok: true, deleted: true } });
      }
      return route.fulfill({ json: { ok: true, profiles } });
    },
  );
  await page.route("**/api/web-search/settings", async (route: Route) => {
    if (route.request().method() === "PATCH") {
      log.push(route.request().postDataJSON() as Record<string, unknown>);
    }
    return route.fulfill({ json: SETTINGS });
  });
  await page.route("**/api/web-search/browser/selftest", (route: Route) =>
    route.fulfill({
      json: {
        ok: true,
        url: "https://bot.sannysoft.com/",
        channel: "chrome",
        headless: false,
        probe: { webdriver: false, timezone: "Europe/Moscow" },
        failed: [],
        checked: 31,
      },
    }),
  );
}

test("profiles, proxies and the self-check", async ({ page, context }) => {
  const log: Record<string, unknown>[] = [];
  await setAuthCookie(context);
  await setup(page, log);
  await page.goto("/settings/browser");

  await expect(page.getByText("Чтение страниц")).toBeVisible();
  await expect(page.getByText(/3\.0 МБ .* прокси res-msk/)).toBeVisible();

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Очистить" }).click();
  await expect(page.getByText("Профиль очищен.")).toBeVisible();
  expect(log[0]).toEqual({ deleted: "reader" });

  await page.getByRole("button", { name: "Добавить прокси" }).click();
  await page.getByLabel("Адрес прокси").nth(1).fill("http://mob.example:9000");
  await page.getByLabel("Пароль прокси").nth(1).fill("new-pass");
  await page.getByRole("button", { name: "Сохранить прокси" }).click();
  await expect(page.getByText("Прокси сохранены.")).toBeVisible();
  const saved = log[1].proxies as Record<string, unknown>[];
  expect(saved[0]).not.toHaveProperty("password");
  expect(saved[1]).toMatchObject({
    id: "proxy-2",
    server: "http://mob.example:9000",
    password: "new-pass",
  });

  await page
    .getByRole("button", { name: "Проверить, что видят сайты" })
    .click();
  await expect(page.getByText(/Проверок: 31, не пройдено: 0/)).toBeVisible();
});

test("the browser settings fit a phone screen", async ({ page, context }) => {
  await setAuthCookie(context);
  await setup(page, []);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/settings/browser");
  await expect(page.getByText("Как сайты видят сотрудника")).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
