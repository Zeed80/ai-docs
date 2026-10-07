/**
 * «Доступ моделей к данным» на «Модели → Назначение»: включение только через
 * подтверждение, выключение — сразу.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test data-access-switch --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

function state(enabled: boolean) {
  return {
    sql_full_access: enabled,
    secret_tables: ["api_keys", "mailbox_configs", "provider_instances"],
    secret_columns: { chat_sessions: ["share_token"] },
  };
}

async function setup(page: Page, puts: unknown[]) {
  await mockEmptyApi(page);
  let enabled = false;
  await page.route("**/api/providers/policy/data-access", async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as { enabled: boolean };
      puts.push(body);
      enabled = body.enabled;
    }
    return route.fulfill({ json: state(enabled) });
  });
}

test("full data access needs a confirmation to turn on and none to turn off", async ({
  page,
  context,
}) => {
  const puts: unknown[] = [];
  await setAuthCookie(context);
  await setup(page, puts);
  await page.goto("/settings/models?tab=assignment");
  const box = page.getByRole("checkbox", { name: /Полный доступ к данным/ });
  await expect(box).not.toBeChecked();
  await expect(page.getByText(/provider_instances/)).toBeVisible();

  page.once("dialog", (d) => d.dismiss());
  await box.click();
  await expect(box).not.toBeChecked();
  expect(puts).toEqual([]);

  page.once("dialog", (d) => {
    expect(d.message()).toContain("облачные");
    void d.accept();
  });
  await box.click();
  await expect(box).toBeChecked();
  expect(puts).toEqual([{ enabled: true, acknowledged: true }]);

  await box.click();
  await expect(box).not.toBeChecked();
  expect(puts[1]).toEqual({ enabled: false, acknowledged: false });
});

test("the data access card fits a 320 px screen", async ({ page, context }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/models?tab=assignment");
  await expect(page.getByRole("checkbox", { name: /Полный доступ к данным/ })).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
