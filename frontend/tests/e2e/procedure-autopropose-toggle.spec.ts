/**
 * «Настройки → Агент → Память»: флажок «Предлагать процедуры…» сохраняется
 * щелчком. Раньше он ждал кнопку «Сохранить настройки агента» в самом низу,
 * и уход со страницы его сбрасывал (владелец, 2026-10-23).
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test procedure-autopropose-toggle --project=chromium
 */

import { expect, test, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";
import agentConfig from "./fixtures/agent-config-defaults.json";

test("the autopropose flag saves on click", async ({ page, context }) => {
  const patches: Record<string, unknown>[] = [];
  let current = { ...agentConfig, procedure_autopropose_enabled: false };
  await setAuthCookie(context);
  await mockEmptyApi(page);
  await page.route("**/api/ai/agent-config", async (route: Route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      patches.push(body);
      current = { ...current, ...body };
    }
    return route.fulfill({ json: current });
  });
  await page.goto("/settings");

  const flag = page.getByRole("checkbox", { name: /Предлагать процедуры/ });
  await expect(flag).not.toBeChecked();
  await flag.click();
  await expect.poll(() => patches).toEqual([{ procedure_autopropose_enabled: true }]);
  await expect(flag).toBeChecked();

  await page.reload();
  await expect(page.getByRole("checkbox", { name: /Предлагать процедуры/ })).toBeChecked();
});
