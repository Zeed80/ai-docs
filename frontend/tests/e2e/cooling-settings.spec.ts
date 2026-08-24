import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([
    { name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" },
  ]);
}

const CHANNELS = [
  {
    id: "gpu:0:fan0",
    label: "NVIDIA GeForce RTX 3090 · вентилятор 1",
    kind: "gpu",
    controllable: true,
    control_reason: null,
    min_pct: 30,
    max_pct: 100,
    has_tach: false,
    default_sensor: "gpu",
    rpm: null,
    pwm_pct: 62,
    mode: "auto",
    failed_reason: null,
    target_pct: null,
    config: null,
  },
  {
    id: "hwmon:nct6687:pwm1",
    label: "NCT6687 · канал 1",
    kind: "mobo",
    controllable: false,
    control_reason:
      "штатный драйвер nct6683 отдаёт pwm только на чтение; нужен DKMS-модуль nct6687d",
    min_pct: 25,
    max_pct: 100,
    has_tach: true,
    default_sensor: "cpu",
    rpm: 1044,
    pwm_pct: 61,
    mode: "auto",
    failed_reason: null,
    target_pct: null,
    config: null,
  },
];

function fansPayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    control_enabled: true,
    hwmon_allowed: false,
    config: { enabled: false, preset: "balanced", channels: {} },
    presets: {
      silent: { label: "Тихий", curves: { gpu: [{ t: 40, pct: 30 }, { t: 83, pct: 100 }] } },
      balanced: { label: "Баланс", curves: { gpu: [{ t: 35, pct: 30 }, { t: 80, pct: 100 }] } },
      max: { label: "Максимум", curves: { gpu: [{ t: 0, pct: 100 }] } },
    },
    custom_presets: {},
    channels: CHANNELS,
    temperatures: { gpu: 59, cpu: 61 },
    emergency: {},
    loop: { running: true, tick_s: 2, last_tick_ts: 1, last_error: null },
    safety: {
      hard_floor_pct: 20,
      emergency_c: { gpu: 83, cpu: 90 },
      emergency_hold_s: 60,
      temp_hysteresis_c: 3,
      max_step_down_pct: 8,
    },
    ...overrides,
  };
}

async function mockCooling(page: Page, payload: Record<string, unknown>, seen: string[]) {
  // The settings shell resolves the signed-in user before rendering anything.
  await page.route("**/api/auth/me", (route: Route) =>
    route.fulfill({
      json: {
        sub: "e2e",
        email: "e2e@example.local",
        name: "E2E User",
        preferred_username: "e2e",
        roles: ["admin"],
        groups: [],
      },
    }),
  );
  await page.route("**/api/cooling/**", async (route: Route) => {
    const url = route.request().url();
    const method = route.request().method();
    seen.push(`${method} ${url.split("/api/cooling")[1]}`);
    if (url.includes("/fans/events")) {
      return route.fulfill({ json: { events: [] } });
    }
    if (url.includes("/fans/preview")) {
      return route.fulfill({
        json: { ok: true, preview: { "gpu:0:fan0": [{ t: 40, pct: 30 }, { t: 80, pct: 100 }] } },
      });
    }
    if (url.includes("/fans/mode")) {
      return route.fulfill({ json: { ok: true, reverted: ["gpu:0:fan0"] } });
    }
    if (method === "GET" && url.includes("/fans")) {
      return route.fulfill({ json: payload });
    }
    return route.fulfill({ json: { ok: true } });
  });
}

test.describe("Настройки → Охлаждение", () => {
  test("показывает каналы и объясняет, почему плата не управляется", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload(), seen);
    await page.goto("/settings/cooling");

    await expect(page.getByRole("heading", { name: "Охлаждение и вентиляторы" })).toBeVisible();
    await expect(page.getByText("gpu:0:fan0")).toBeVisible();
    await expect(page.getByText("управляем (30–100%)")).toBeVisible();
    await expect(page.getByText(/нужен DKMS-модуль nct6687d/)).toBeVisible();
    await expect(page.getByText("1044 об/мин")).toBeVisible();
  });

  test("проверка кривой не пишет в железо", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload(), seen);
    await page.goto("/settings/cooling");

    await page.getByRole("button", { name: "Проверить (без записи)" }).click();
    await expect(page.getByText("Проверка выполнена — железо не тронуто.")).toBeVisible();
    expect(seen.some((c) => c.startsWith("POST /fans/preview"))).toBe(true);
    expect(seen.some((c) => c.startsWith("POST /fans/config"))).toBe(false);
    expect(seen.some((c) => c.startsWith("POST /fans/manual"))).toBe(false);
  });

  test("возврат прошивке доступен всегда", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload({ control_enabled: false }), seen);
    await page.goto("/settings/cooling");

    await expect(page.getByText(/Управление выключено/)).toBeVisible();
    const revert = page.getByRole("button", { name: "Вернуть всё прошивке" });
    await expect(revert).toBeEnabled();
    await revert.click();
    await expect(page.getByText(/возвращены под управление прошивки/)).toBeVisible();
  });

  test("аварийный обгон виден отдельным баннером", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload({ emergency: { gpu: 42 } }), seen);
    await page.goto("/settings/cooling");

    await expect(page.getByText("Аварийный обгон активен.")).toBeVisible();
    await expect(page.getByText(/GPU: ещё 42 с/)).toBeVisible();
  });
});
