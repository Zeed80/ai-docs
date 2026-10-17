/**
 * Переключатель видеокарты Ollama ⇄ Strata на «Модели → Провайдеры».
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test strata-gpu-switch --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const deleted: string[] = [];

const QUANTS = [
  ["qwen", "Q2_0", "Q2_0 — самая быстрая", 66.4, 34.0],
  ["qwen", "IQ2_XS", "IQ2_XS — рекомендуемая", 68.0, 35.5],
  ["qwen", "IQ3_XXS", "IQ3_XXS — точнее, медленнее", 75.8, 42.9],
  ["qwen", "IQ3_S", "IQ3_S — лучшее качество", 83.6, 50.3],
  ["qwen36", "UD-Q4_K_XL", "UD-Q4_K_XL — 4 бита, самая лёгкая", 22.9, 21.0],
] as const;

const vid = (family: string, model: string) =>
  family === "qwen" ? model : `${family}-${model}`;

function status(over: Record<string, unknown> = {}) {
  return {
    owner: "ollama",
    container: "exited",
    docker_error: null,
    phase: "stopped",
    down_while_owner: false,
    health: null,
    server: {
      current: {
        version: "0.1.43.1",
        commit: "a0116b2",
        image: "ai-workspace/strata:a0116b2",
        image_id: "sha256:x",
      },
      latest: {
        version: "0.1.45",
        tag: "v0.1.45",
        commit: "477ac13",
        notes: "Qwen3.6-35B-A3B reads pictures",
      },
      update_available: true,
      job: null,
    },
    installer: null,
    desired: {
      id: "IQ3_S",
      family: "qwen",
      model: "IQ3_S",
      context: 65536,
      vision: true,
      reinstall_pending: false,
      install_only: false,
      low_ram: "auto",
    },
    installed: ["IQ3_S", "IQ2_XS", "qwen36-UD-Q4_K_XL"],
    installed_context: 65536,
    quants: QUANTS.map(([family, model, label, download_gb, experts_gb]) => {
      const id = vid(family, model);
      const installed = ["IQ3_S", "IQ2_XS", "qwen36-UD-Q4_K_XL"].includes(id);
      return {
        id,
        family,
        model,
        label,
        download_gb,
        disk_need_gb: download_gb + experts_gb + 7,
        experts_gb,
        ram_gb: 60,
        installed,
        size_on_disk_gb:
          id === "IQ3_S"
            ? 90.1
            : id === "IQ2_XS"
              ? 65.5
              : installed
                ? 23.8
                : null,
        low_ram_mode: id === "IQ3_S",
        ram_mode:
          id === "IQ3_S" ? "resident" : family === "qwen36" ? "own" : "off",
        fits_ram: true,
        catalog_key:
          family === "qwen36"
            ? "strata_qwen3_6_35b"
            : "strata_qwen3_8_flash_next",
        min_version: family === "qwen36" ? "0.1.45" : null,
        installed_settings: installed ? { context: 65536, vision: true } : null,
      };
    }),
    families: [
      {
        family: "qwen",
        title: "Qwen3.8-Flash-Next",
        about: "исходная модель, 125B MoE",
        catalog_key: "strata_qwen3_8_flash_next",
      },
      {
        family: "qwen36",
        title: "Qwen3.6-35B-A3B",
        about: "35B MoE, 3B активных",
        catalog_key: "strata_qwen3_6_35b",
      },
    ],
    served_catalog_key: "strata_qwen3_8_flash_next",
    ram_plan: {
      reserve_gb: 0,
      limit_gb: null,
      low_ram: "auto",
      resident_budget_gib: null,
      mode: "resident",
      fits: true,
    },
    reserve_choices: [0, 4, 8, 12, 16, 24, 32],
    contexts: [32768, 65536, 131072],
    disk_free_gb: 395,
    ram_total_gb: 59.2,
    log_tail: [],
    switch_revision: null,
    runtime: {
      idle_unload_s: 300,
      free_comfyui: true,
      min_free_vram_mib: 12000,
      parallel: 2,
      conversation_cache_mib: 6144,
      ram_reserve_gb: 0,
    },
    idle_choices: [0, 60, 300, 600, 1800],
    parallel_choices: [1, 2],
    conversation_cache_choices: [0, 2048, 4096, 6144, 8192],
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

const calls: string[] = [];

async function setup(
  page: Page,
  switchBodies: unknown[],
  runtimeBodies: unknown[] = [],
  statusOver: Record<string, unknown> = {},
) {
  calls.length = 0;
  await mockEmptyApi(page);
  await page.route("**/api/local-models/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === "/api/local-models/status")
      return route.fulfill({ json: ALL_STATUS });
    if (path === "/api/local-models/strata/status")
      return route.fulfill({ json: status(statusOver) });
    if (path === "/api/local-models/strata/load-plan")
      return route.fulfill({
        json: {
          id: url.searchParams.get("id"),
          catalog_key: "strata_qwen3_6_35b",
          context: 65536,
          vision: true,
          setup_pass: false,
          ram_plan: status().ram_plan,
          follow: [
            {
              slot: "ocr_fast",
              label: "Быстрая (OCR/VLM)",
              current_model: "strata_qwen3_8_flash_next",
              move: true,
              reason: "→ strata_qwen3_6_35b",
            },
          ],
          warnings: [],
        },
      });
    if (
      [
        "/api/local-models/strata/load",
        "/api/local-models/strata/install",
        "/api/local-models/strata/update",
        "/api/local-models/strata/server",
      ].includes(path)
    ) {
      calls.push(
        `${method} ${path} ${route.request().postData() ?? ""}`.trim(),
      );
      return route.fulfill({
        json: {
          ok: true,
          moved: ["ocr_fast"],
          restarted: false,
          status: status(),
        },
      });
    }
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
              parallel: 1,
              conversation_cache_mib: 4096,
              ram_reserve_gb: 0,
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
  await page.getByLabel("Параллельные запросы").selectOption("1");
  await page.getByLabel("Кэш разговоров").selectOption("4096");
  const residency = page.getByText("Работа модели").locator("..");
  await residency.getByRole("button", { name: "Применить" }).click();
  await expect.poll(() => runtimeBodies.length).toBe(1);
  expect(runtimeBodies[0]).toEqual({
    idle_unload_s: 600,
    free_comfyui: true,
    parallel: 1,
    conversation_cache_mib: 4096,
  });

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

test("an available update is offered and started with one button", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/models?tab=overview");
  await expect(page.getByText("0.1.43.1")).toBeVisible();
  await expect(page.getByText("доступна 0.1.45")).toBeVisible();
  await page.getByRole("button", { name: "Что нового" }).click();
  await expect(page.getByText("Qwen3.6-35B-A3B reads pictures")).toBeVisible();
  await page.getByRole("button", { name: "Обновить Strata до 0.1.45" }).click();
  await expect
    .poll(() => calls)
    .toEqual(["POST /api/local-models/strata/update"]);
});

test("a downloaded model of another family is loaded after a plan", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const base = status().server;
  await setup(page, [], [], {
    server: {
      ...base,
      current: { ...base.current, version: "0.1.45" },
      update_available: false,
    },
  });
  await page.goto("/settings/models?tab=overview");
  await page
    .getByRole("button", { name: "Загрузить qwen36-UD-Q4_K_XL" })
    .click();
  await expect(
    page.getByText(/Перейдут на strata_qwen3_6_35b: Быстрая \(OCR\/VLM\)/),
  ).toBeVisible();
  expect(calls).toEqual([]);
  await page.getByRole("button", { name: "Да, загрузить" }).click();
  await expect
    .poll(() => calls)
    .toEqual(['POST /api/local-models/strata/load {"id":"qwen36-UD-Q4_K_XL"}']);
});

test("a model that is not downloaded is fetched beside the server", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/models?tab=overview");
  await page.getByRole("button", { name: "Скачать IQ3_XXS" }).click();
  await expect
    .poll(() => calls)
    .toEqual([
      'POST /api/local-models/strata/install {"id":"IQ3_XXS","context":65536,"vision":true}',
    ]);
});

test("the RAM reserve is saved", async ({ page, context }) => {
  await setAuthCookie(context);
  const runtimeBodies: unknown[] = [];
  await setup(page, [], runtimeBodies);
  await page.goto("/settings/models?tab=overview");
  await page.getByLabel("Оставлять системе").selectOption("12");
  const block = page.getByText("Оперативная память").locator("..");
  await block.getByRole("button", { name: "Применить" }).click();
  await expect.poll(() => runtimeBodies).toEqual([{ ram_reserve_gb: 12 }]);
});

test("without a container Strata is offered for install", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await setup(page, [], [], {
    phase: "not_created",
    container: null,
    server: {
      current: null,
      latest: { version: "0.1.45" },
      update_available: false,
      job: null,
    },
  });
  await page.goto("/settings/models?tab=overview");
  await expect(page.getByText("Strata не установлена")).toBeVisible();
  // Без контейнера скачивать модели некуда: списка нет.
  await expect(page.getByRole("button", { name: /^Скачать / })).toHaveCount(0);
  await page.getByRole("button", { name: "Установить Strata" }).click();
  await expect
    .poll(() => calls)
    .toEqual(["POST /api/local-models/strata/server"]);
});

test("a family newer than the installed Strata asks for the update first", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/models?tab=overview");
  await expect(
    page.getByRole("button", { name: "Загрузить qwen36-UD-Q4_K_XL" }),
  ).toBeDisabled();
  await expect(
    page.getByText("нужна Strata 0.1.45 или новее").first(),
  ).toBeVisible();
});
