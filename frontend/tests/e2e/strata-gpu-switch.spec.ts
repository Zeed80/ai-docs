/**
 * Переключатель видеокарты Ollama ⇄ Strata на «Модели → Провайдеры».
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test strata-gpu-switch --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const deleted: string[] = [];

const QUANTS = [
  ["Q2_0", "Q2_0 — самая быстрая", 66.4, 34.0],
  ["IQ2_XS", "IQ2_XS — рекомендуемая", 68.0, 35.5],
  ["IQ3_XXS", "IQ3_XXS — точнее, медленнее", 75.8, 42.9],
  ["IQ3_S", "IQ3_S — лучшее качество", 83.6, 50.3],
] as const;

function status(over: Record<string, unknown> = {}) {
  return {
    owner: "ollama",
    container: "exited",
    docker_error: null,
    phase: "stopped",
    down_while_owner: false,
    health: null,
    desired: {
      model: "IQ3_S",
      context: 65536,
      vision: true,
      reinstall_pending: false,
      install_only: false,
    },
    installed: ["IQ3_S", "IQ2_XS"],
    quants: QUANTS.map(([model, label, download_gb, experts_gb]) => ({
      model,
      label,
      download_gb,
      disk_need_gb: download_gb + experts_gb + 7,
      experts_gb,
      ram_gb: 60,
      installed: model === "IQ3_S" || model === "IQ2_XS",
      size_on_disk_gb:
        model === "IQ3_S" ? 90.1 : model === "IQ2_XS" ? 65.5 : null,
      low_ram_mode: model === "IQ3_S",
    })),
    contexts: [32768, 65536, 131072],
    disk_free_gb: 395,
    ram_total_gb: 59.2,
    log_tail: [],
    switch_revision: null,
    runtime: {
      idle_unload_s: 300,
      free_comfyui: true,
      min_free_vram_mib: 12000,
    },
    idle_choices: [0, 60, 300, 600, 1800],
    external_url: "http://192.168.1.246:8090",
    ...over,
  };
}

const PLAN = [
  {
    slot: "ocr_fast",
    label: "Быстрая (OCR/VLM)",
    current_model: "qwen3_5_9b_ollama",
    move: true,
    reason: "GPU-Ollama → Strata",
  },
  {
    slot: "agent_orchestrator",
    label: "Оркестратор",
    current_model: "ollama_thinkingcap_qwen3_8_27b_65536",
    move: true,
    reason: "GPU-Ollama → Strata",
  },
  {
    slot: "embedding",
    label: "Векторизация (embedding)",
    current_model: "qwen3_embedding_4b_ollama",
    move: false,
    reason: "не на GPU-Ollama — переключение его не касается",
  },
];

const ALL_STATUS = {
  providers: {
    ollama: { available: true, models: [], loaded: [] },
    llamacpp: { available: false, models: [], loaded: [] },
    vllm: { available: false, models: [], loaded: [] },
  },
  gpu: null,
  vram_allocations: {},
  total_vram_gb: 24,
};

async function setup(
  page: Page,
  switchBodies: unknown[],
  runtimeBodies: unknown[] = [],
) {
  await mockEmptyApi(page);
  await page.route("**/api/local-models/**", async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/local-models/status")
      return route.fulfill({ json: ALL_STATUS });
    if (path === "/api/local-models/strata/status")
      return route.fulfill({ json: status() });
    if (path === "/api/local-models/strata/slot-plan")
      return route.fulfill({ json: PLAN });
    if (path.startsWith("/api/local-models/strata/quants/")) {
      deleted.push(`${route.request().method()} ${path}`);
      return route.fulfill({
        json: {
          ok: true,
          freed_gb: 65.5,
          status: status({ installed: ["IQ3_S"] }),
        },
      });
    }
    if (path === "/api/local-models/strata/access")
      return route.fulfill({
        json: {
          url: "http://192.168.1.246:8090",
          openai_base_url: "http://192.168.1.246:8090/v1",
          api_key: "k-123",
        },
      });
    if (path === "/api/local-models/strata/runtime") {
      runtimeBodies.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          ok: true,
          restarted: true,
          status: status({
            runtime: {
              idle_unload_s: 600,
              free_comfyui: true,
              min_free_vram_mib: 12000,
            },
          }),
        },
      });
    }
    if (path === "/api/local-models/strata/switch") {
      switchBodies.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          ok: true,
          moved: ["agent_orchestrator"],
          restored: [],
          ollama_unloaded: ["qwen3.5:9b"],
          status: status({
            owner: "strata",
            container: "running",
            phase: "loading",
          }),
        },
      });
    }
    return route.fulfill({ json: { items: [], total: 0 } });
  });
}

test("switching to Strata shows the slot plan and sends only the chosen slots", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const bodies: unknown[] = [];
  await setup(page, bodies);
  await page.goto("/settings/models?tab=overview");

  const group = page.getByRole("radiogroup", {
    name: "Кому отдана видеокарта",
  });
  await expect(group.getByRole("radio", { name: "Ollama" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await group.getByRole("radio", { name: "Strata" }).click();

  // Слот на CPU-узле показан, но переносить его нельзя.
  const embedding = page.getByLabel(/Векторизация \(embedding\)/);
  await expect(embedding).toBeDisabled();
  await page.getByLabel(/Быстрая \(OCR\/VLM\)/).uncheck();

  await page.getByRole("button", { name: "Отдать видеокарту Strata" }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toEqual({
    target: "strata",
    move_slots: ["agent_orchestrator"],
  });
  await expect(page.getByText("Strata: загрузка в память")).toBeVisible();
  await expect(group.getByRole("radio", { name: "Strata" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});

test("the Strata card fits a 320 px screen", async ({ page, context }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/models?tab=overview");
  await expect(page.getByText("IQ3_S — лучшее качество")).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test("idle unload is saved and the LAN key is shown on request", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const runtimeBodies: unknown[] = [];
  await setup(page, [], runtimeBodies);
  await page.goto("/settings/models?tab=overview");

  await page.getByLabel("Выгружать модель после простоя").selectOption("600");
  const residency = page.getByText("Выгрузка при простое").locator("..");
  await residency.getByRole("button", { name: "Применить" }).click();
  await expect.poll(() => runtimeBodies.length).toBe(1);
  expect(runtimeBodies[0]).toEqual({ idle_unload_s: 600, free_comfyui: true });

  await expect(page.getByText("http://192.168.1.246:8090/v1")).toBeVisible();
  await expect(page.getByText("k-123")).toHaveCount(0);
  await page.getByRole("button", { name: "Показать ключ" }).click();
  await expect(page.getByText("k-123")).toBeVisible();
});

test("a downloaded quant that is not selected can be deleted after a confirm", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  deleted.length = 0;
  await setup(page, []);
  await page.goto("/settings/models?tab=overview");

  // The selected quant (IQ3_S) has no delete control; the other downloaded one has.
  await expect(page.getByRole("button", { name: "Удалить IQ3_S" })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Удалить IQ2_XS" }).click();
  await expect(
    page.getByText("Удалить IQ2_XS с диска (65.5 ГБ)?"),
  ).toBeVisible();
  expect(deleted).toEqual([]);
  await page.getByRole("button", { name: "Да, удалить" }).click();
  await expect
    .poll(() => deleted)
    .toEqual(["DELETE /api/local-models/strata/quants/IQ2_XS"]);
});
