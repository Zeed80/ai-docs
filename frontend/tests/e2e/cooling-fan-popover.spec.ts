import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([
    { name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" },
  ]);
}

const TELEMETRY = {
  available: true,
  ts: 1,
  gpu: {
    name: "NVIDIA GeForce RTX 3090",
    driver_version: "595.71.05",
    utilization_pct: 40,
    temp_gpu_c: 59,
    temp_mem_c: null,
    temp_mem_junction_c: 70,
    power_draw_w: 180,
    power_limit_w: 195,
    power_limit_min_w: 100,
    power_limit_max_w: 375,
    power_limit_default_w: 350,
    fan_pct: 62,
    vram_total_gb: 24,
    vram_used_gb: 8,
    vram_free_gb: 16,
    clock_sm_mhz: 1700,
    clock_mem_mhz: 9500,
    source: "sidecar",
  },
  cpu: null,
};

function fans(overrides: Record<string, unknown> = {}) {
  return {
    control_enabled: true,
    config: { enabled: true, preset: "balanced" },
    presets: { silent: { label: "Тихий" }, balanced: { label: "Баланс" }, max: { label: "Максимум" } },
    custom_presets: {},
    channels: [
      {
        id: "gpu:0:fan0", label: "RTX 3090 · вентилятор 1", kind: "gpu",
        controllable: true, control_reason: null, min_pct: 30, max_pct: 100,
        pwm_pct: 62, rpm: null, mode: "auto",
      },
      {
        id: "gpu:0:fan1", label: "RTX 3090 · вентилятор 2", kind: "gpu",
        controllable: true, control_reason: null, min_pct: 30, max_pct: 100,
        pwm_pct: 62, rpm: null, mode: "auto",
      },
      {
        id: "hwmon:nct6687:pwm1", label: "NCT6687 · канал 1", kind: "mobo",
        controllable: false,
        control_reason: "штатный драйвер nct6683 отдаёт pwm только на чтение; нужен DKMS-модуль nct6687d",
        min_pct: 25, max_pct: 100, pwm_pct: 61, rpm: 1044, mode: "auto",
      },
    ],
    ...overrides,
  };
}

async function mockAll(page: Page, seen: string[], fansBody: Record<string, unknown>) {
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const p = url.pathname;
    if (p.startsWith("/api/cooling")) seen.push(`${method} ${p}`);

    if (p === "/api/auth/me") {
      return route.fulfill({
        json: { sub: "e2e", email: "e2e@x", name: "E2E", preferred_username: "e2e", roles: ["admin"], groups: [] },
      });
    }
    if (p === "/api/local-models/gpu-telemetry") return route.fulfill({ json: TELEMETRY });
    if (p === "/api/cooling/fans") return route.fulfill({ json: fansBody });
    if (p.startsWith("/api/cooling/")) return route.fulfill({ json: { ok: true, applied_pct: 45, reverted: [] } });
    // The assistant shell reads several list endpoints; an object would crash it.
    return route.fulfill({ json: [] });
  });
}

async function openFanPopover(page: Page) {
  await page.goto("/assistant");
  // The assistant route renders the panel twice (page + layout shell), so the
  // fan chip legitimately matches more than once — drive the first one.
  const chip = page.getByRole("button", { name: /^F 62%$/ }).first();
  await chip.waitFor();
  await chip.click();
}

test.describe("Всплывающее окно вентиляторов", () => {
  test("открывается с индикатора и показывает пресеты и ползунок", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockAll(page, seen, fans());
    await openFanPopover(page);

    await expect(page.getByText("Вентиляторы GPU")).toBeVisible();
    for (const name of ["Тихий", "Баланс", "Максимум", "Авто"]) {
      await expect(page.getByRole("button", { name, exact: true })).toBeVisible();
    }
    const slider = page.getByRole("slider");
    await expect(slider).toBeVisible();
    await expect(slider).toHaveAttribute("min", "30");
    await expect(slider).toHaveAttribute("max", "100");
    await expect(page.getByRole("link", { name: /Настройки охлаждения/ })).toBeVisible();
  });

  test("пресет уходит на сервер", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockAll(page, seen, fans());
    await openFanPopover(page);

    await page.getByRole("button", { name: "Тихий", exact: true }).click();
    await expect
      .poll(() => seen.some((c) => c === "POST /api/cooling/presets/silent/apply"))
      .toBe(true);
  });

  test("ползунок задаёт обороты каждому вентилятору GPU", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockAll(page, seen, fans());
    await openFanPopover(page);

    await page.getByRole("slider").fill("45");
    await expect
      .poll(() => seen.filter((c) => c === "POST /api/cooling/fans/manual").length)
      .toBe(2); // one call per controllable GPU fan
  });

  test("при выключенном управлении объясняет причину вместо ползунка", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockAll(page, seen, fans({ control_enabled: false }));
    await openFanPopover(page);

    await expect(page.getByText(/Управление выключено/)).toBeVisible();
    await expect(page.getByRole("slider")).toHaveCount(0);
    await expect(page.getByRole("link", { name: /Настройки охлаждения/ })).toBeVisible();
  });
});
