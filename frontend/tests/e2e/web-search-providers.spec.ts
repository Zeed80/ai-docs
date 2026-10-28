/**
 * «Настройки → Веб-поиск» (W1): список поисковиков. Добавленный поисковик
 * уходит в PATCH, сохранённый ключ, который не перевводили, не отправляется,
 * квота показывает расход за сегодня.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test web-search-providers --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const PROVIDER = {
  enabled: true,
  role: "primary",
  weight: 1,
  endpoint: null,
  engines: [],
  region: null,
  folder_id: null,
  mcp_tool: null,
  daily_quota: null,
  used_today: null,
};

const SETTINGS = {
  browser_url: "http://web-browser:8093",
  browsing_enabled: true,
  supported_providers: [
    "searxng",
    "tavily",
    "serper",
    "brave",
    "exa",
    "jina",
    "yandex",
    "custom",
    "mcp",
  ],
  default_endpoints: {
    searxng: "http://searxng:8080/search",
    yandex: "https://searchapi.api.cloud.yandex.net/v2/web/search",
  },
  providers: [
    {
      ...PROVIDER,
      id: "searxng",
      kind: "searxng",
      endpoint_effective: "http://searxng:8080/search",
      api_key_mask: "",
      api_key_set: false,
    },
    {
      ...PROVIDER,
      id: "brave",
      kind: "brave",
      role: "fallback",
      endpoint_effective: "https://api.search.brave.com/res/v1/web/search",
      api_key_mask: "BSA…9f",
      api_key_set: true,
      daily_quota: 100,
      used_today: 7,
    },
  ],
  providers_saved: true,
  language: "ru",
  per_host_interval_s: 1.5,
  search_cache_ttl_s: 21600,
  page_cache_ttl_s: 86400,
  research_concurrency: 4,
};

async function setup(page: Page, patches: Record<string, unknown>[]) {
  await mockEmptyApi(page);
  await page.route("**/api/web-search/settings", async (route: Route) => {
    if (route.request().method() === "PATCH") {
      patches.push(route.request().postDataJSON() as Record<string, unknown>);
      return route.fulfill({ json: SETTINGS });
    }
    return route.fulfill({ json: SETTINGS });
  });
}

test("providers are listed, added and saved without re-sending stored keys", async ({
  page,
  context,
}) => {
  const patches: Record<string, unknown>[] = [];
  await setAuthCookie(context);
  await setup(page, patches);
  await page.goto("/settings/web-search");

  await expect(page.getByText("Поисковики", { exact: true })).toBeVisible();
  await expect(page.getByText("сегодня: 7 из 100")).toBeVisible();
  await expect(
    page.getByText("(сохранён: BSA…9f; пусто — оставить)"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Добавить поисковик" }).click();
  const fresh = page
    .locator("div.rounded-lg")
    .filter({ hasText: "Каталог (folderId)" });
  await fresh.getByPlaceholder("b1g…").fill("b1gfolder");
  await fresh.getByPlaceholder("••••••••").fill("AQVN-new-key");

  await page.getByRole("button", { name: "Сохранить" }).click();
  await expect(page.getByText("Настройки сохранены.")).toBeVisible();

  const providers = patches[0].providers as Record<string, unknown>[];
  expect(providers.map((p) => p.id)).toEqual([
    "searxng",
    "brave",
    "provider-3",
  ]);
  expect(providers[1]).not.toHaveProperty("api_key");
  expect(providers[1].daily_quota).toBe(100);
  expect(providers[2]).toMatchObject({
    kind: "yandex",
    folder_id: "b1gfolder",
    api_key: "AQVN-new-key",
  });
});

test("the settings page fits a phone screen", async ({ page, context }) => {
  await setAuthCookie(context);
  await setup(page, []);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/settings/web-search");
  await expect(page.getByText("Поисковики", { exact: true })).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
