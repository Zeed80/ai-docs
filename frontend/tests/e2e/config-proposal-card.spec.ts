/**
 * «Настройки → Агент»: защищённое предложение говорит, что изменит, помечено
 * как устаревшее, а критичное применяется только вторым нажатием.
 * 2026-10-21 тестовое предложение system_prompt двухмесячной давности
 * одобрили вместе с другими, и оно заменило весь основной промпт агента.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test config-proposal-card --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";
import agentConfig from "./fixtures/agent-config-defaults.json";

const PROPOSAL = {
  id: "p1",
  setting_path: "system_prompt",
  proposed_value: { value: "ЖИВАЯ ПРОВЕРКА — не должно примениться" },
  current_value: { value: null },
  reason: "Ф7 live check regression guard",
  risk_level: "critical",
  protected: true,
  status: "pending",
  requested_by: "sveta",
  decided_by: null,
  decided_at: null,
  decision_comment: null,
  created_at: "2026-08-20T17:19:07Z",
  impact:
    "Заменит ВЕСЬ основной промпт агента (base.md) этим текстом: правила работы, писем и подтверждений перестанут действовать. Пустое значение возвращает основной промпт.",
  age_days: 62,
  stale: true,
  current_changed: false,
};

async function setup(page: Page, decisions: unknown[]) {
  await mockEmptyApi(page);
  await page.route("**/api/ai/agent-config", (route: Route) =>
    route.fulfill({ json: agentConfig }),
  );
  await page.route("**/api/agent/config/proposals?status=pending", (route: Route) =>
    route.fulfill({ json: decisions.length ? [] : [PROPOSAL] }),
  );
  await page.route(
    "**/api/agent/config/proposals/*/decide",
    async (route: Route) => {
      decisions.push(route.request().postDataJSON());
      return route.fulfill({ json: { ...PROPOSAL, status: "approved" } });
    },
  );
}

test("a critical proposal explains itself and asks twice", async ({
  page,
  context,
}) => {
  const decisions: unknown[] = [];
  await setAuthCookie(context);
  await setup(page, decisions);
  await page.goto("/settings");

  const card = page.getByRole("region", { name: "Предложение system_prompt" });
  await expect(card.getByText("критично")).toBeVisible();
  await expect(card.getByText(/Заменит ВЕСЬ основной промпт/)).toBeVisible();
  await expect(card.getByText("устарело: ждёт 62 дн.")).toBeVisible();

  await card.getByRole("button", { name: "Разрешить" }).click();
  expect(decisions).toHaveLength(0);
  await card.getByRole("button", { name: "Отмена" }).click();
  await card.getByRole("button", { name: "Разрешить" }).click();
  await card.getByRole("button", { name: "Да, применить" }).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(decisions[0]).toMatchObject({ approved: true });
});
