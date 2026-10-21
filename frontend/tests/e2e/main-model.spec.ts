/**
 * «Модели → Назначение»: основная модель вверху переназначает каждый слот,
 * который её использует; эмбеддинг выбирается отдельно.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test main-model --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

function model(key: string, name: string) {
  return {
    key,
    provider: "ollama",
    provider_model: name,
    modalities: ["text", "vision", "tool_calling"],
    local_only: true,
    vram_gb_estimate: null,
    status: "production",
    availability: "available",
    loaded: true,
    thinking_supported: false,
    thinking_enabled: false,
  };
}

function slot(
  id: string,
  group: string,
  label: string,
  extra: Record<string, unknown>,
) {
  return {
    slot: id,
    group,
    label,
    hint: "",
    model: "m_a",
    current_model: "m_a",
    local_only: true,
    required_modality: null,
    ...extra,
  };
}

const SLOTS = [
  slot("agent_orchestrator", "Агент", "Сотрудник", {
    can_follow_main: true,
    follows_main: true,
  }),
  slot("agent_email", "Агент", "Письма", {
    can_follow_main: true,
    follows_main: true,
  }),
  slot("ocr_fast", "Документы", "Быстрая (OCR/VLM)", {
    can_follow_main: true,
    follows_main: false,
    model: "m_b",
    current_model: "m_b",
  }),
  slot("embedding", "Поиск", "Векторизация (embedding)", {
    can_follow_main: false,
    follows_main: false,
    model: "m_e",
    current_model: "m_e",
  }),
];

async function setup(page: Page, applied: Record<string, unknown>[]) {
  await mockEmptyApi(page);
  await page.route("**/api/providers/live-models", (route: Route) =>
    route.fulfill({
      json: [
        model("m_a", "model-a"),
        model("m_b", "model-b"),
        model("m_e", "embed-e"),
      ],
    }),
  );
  await page.route("**/api/providers/assignment-draft", (route: Route) =>
    route.fulfill({ json: { slots: SLOTS, main_model: "m_a", diff: [] } }),
  );
  await page.route(
    "**/api/providers/assignment-draft/apply",
    async (route: Route) => {
      applied.push(route.request().postDataJSON());
      return route.fulfill({
        json: { slots: SLOTS, main_model: "m_b", diff: [] },
      });
    },
  );
}

test("the main model reassigns every slot that uses it", async ({
  page,
  context,
}) => {
  const applied: Record<string, unknown>[] = [];
  await setAuthCookie(context);
  await setup(page, applied);
  await page.goto("/settings/models");

  const main = page.getByRole("region", { name: "Основная модель" });
  await expect(main.getByText("2 из 3 слотов используют её")).toBeVisible();
  await expect(page.getByLabel("Использовать основную модель")).toHaveCount(3);

  await main.getByRole("button", { name: /model-a/ }).click();
  await page.getByRole("option", { name: /model-b/ }).click();
  await expect(main.getByText("черновик")).toBeVisible();

  // ocr_fast did not follow: ticking it puts it on the new main model too.
  await page.getByLabel("Использовать основную модель").nth(2).check();
  await page.getByRole("button", { name: "Применить" }).click();

  await expect.poll(() => applied.length).toBe(1);
  const body = applied[0] as {
    main_model: string;
    slots: Record<string, Record<string, unknown>>;
  };
  expect(body.main_model).toBe("m_b");
  expect(body.slots.agent_orchestrator.model).toBe("m_b");
  expect(body.slots.agent_email.model).toBe("m_b");
  expect(body.slots.ocr_fast).toMatchObject({
    model: "m_b",
    follows_main: true,
  });
  expect(body.slots.embedding.model).toBe("m_e");
});
