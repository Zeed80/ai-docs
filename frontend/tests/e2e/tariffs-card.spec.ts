/**
 * «Модели → Библиотека → Тарифы и расход» (T4): версия тарифа добавляется
 * формой, расход по квитанциям показывает «неизвестно», а не ноль.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test tariffs-card --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const RECEIPTS = [
  {
    provider: "anthropic",
    model: "claude-x",
    currency: "USD",
    calls: 3,
    priced_calls: 2,
    amount: "0.021",
    unknown: { tokens_unknown: 1 },
  },
  {
    provider: "strata",
    model: "qwen3.8-flash-next",
    currency: "USD",
    calls: 5,
    priced_calls: 0,
    amount: "0",
    unknown: { tariff_unavailable: 5 },
  },
];

async function setup(page: Page, posts: unknown[]) {
  await mockEmptyApi(page);
  const tariffs: unknown[] = [];
  await page.route("**/api/providers/tariffs", async (route: Route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      posts.push(body);
      const row = {
        id: `t${posts.length}`,
        ...body,
        effective_from: "2026-10-12T00:00:00Z",
        created_by: "admin",
      };
      tariffs.unshift(row);
      return route.fulfill({ status: 201, json: row });
    }
    return route.fulfill({ json: tariffs });
  });
  await page.route("**/api/providers/cost-report", (route: Route) =>
    route.fulfill({
      json: {
        total_usd: 0,
        priced_models: 0,
        unpriced_models: [],
        by_model: [],
        receipts: RECEIPTS,
      },
    }),
  );
}

test("a tariff version is added and unpriced calls read as unknown", async ({
  page,
  context,
}) => {
  const posts: unknown[] = [];
  await setAuthCookie(context);
  await setup(page, posts);
  await page.goto("/settings/models?tab=library");
  await expect(page.getByText("Тарифы и расход")).toBeVisible();
  await expect(page.getByText("0.021 USD")).toBeVisible();
  await expect(page.getByText("нет тарифа: 5")).toBeVisible();
  await expect(page.getByText("неизвестно", { exact: true })).toBeVisible();

  await page.getByLabel("Провайдер").fill("anthropic");
  await page.getByLabel("Модель", { exact: true }).fill("claude-x");
  await page.getByLabel("Цена входа за 1 млн токенов").fill("3");
  await page.getByLabel("Цена выхода за 1 млн токенов").fill("15");
  await page.getByLabel("Источник цены").fill("прайс провайдера");
  await page.getByRole("button", { name: "Добавить версию" }).click();
  await expect(page.getByText(/3 \/ 15 USD за 1 млн токенов/)).toBeVisible();
  expect(posts).toEqual([
    {
      provider: "anthropic",
      model: "claude-x",
      currency: "USD",
      input_per_1m: "3",
      output_per_1m: "15",
      free: false,
      source: "прайс провайдера",
    },
  ]);
});

test("the tariffs card fits a 320 px screen", async ({ page, context }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/models?tab=library");
  await expect(
    page.getByRole("button", { name: "Добавить версию" }),
  ).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
