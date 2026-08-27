import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([
    {
      name: "access_token",
      value: "e2e-token",
      domain: "127.0.0.1",
      path: "/",
    },
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
    config: { enabled: false, presets: { gpu: "balanced", mobo: "balanced" }, channels: {} },
    presets: {
      silent: {
        label: "Тихий",
        curves: {
          gpu: [
            { t: 40, pct: 30 },
            { t: 83, pct: 100 },
          ],
        },
      },
      balanced: {
        label: "Баланс",
        curves: {
          gpu: [
            { t: 35, pct: 30 },
            { t: 80, pct: 100 },
          ],
        },
      },
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

async function mockCooling(
  page: Page,
  payload: Record<string, unknown>,
  seen: string[],
) {
  // The switches are controlled inputs bound to server state, so the fake has
  // to remember what it was told — otherwise a click appears to do nothing,
  // exactly as it would against a backend that ignored the request.
  const control = {
    control_enabled: !!payload.control_enabled,
    hwmon_allowed: !!payload.hwmon_allowed,
  };
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
    if (url.includes("/control") && method === "POST") {
      const body = route.request().postDataJSON() as {
        enabled?: boolean;
        allow_hwmon?: boolean;
      };
      if (body.enabled !== undefined) control.control_enabled = body.enabled;
      if (body.allow_hwmon !== undefined)
        control.hwmon_allowed = body.allow_hwmon;
      return route.fulfill({ json: { ok: true, ...control } });
    }
    if (url.includes("/fans/events")) {
      return route.fulfill({ json: { events: [] } });
    }
    if (url.includes("/setup-guide")) {
      return route.fulfill({
        json: {
          available: true,
          markdown: [
            "# Управление вентиляторами материнской платы",
            "",
            "## Шаг 1. Диагностика",
            "",
            "Запустите `bash infra/scripts/fan-control-setup.sh` — он ничего не меняет.",
            "",
            "```bash",
            "bash infra/scripts/fan-control-setup.sh",
            "```",
          ].join("\n"),
        },
      });
    }
    if (url.includes("/fans/preview")) {
      return route.fulfill({
        json: {
          ok: true,
          preview: {
            "gpu:0:fan0": [
              { t: 40, pct: 30 },
              { t: 80, pct: 100 },
            ],
          },
        },
      });
    }
    if (url.includes("/fans/mode")) {
      return route.fulfill({ json: { ok: true, reverted: ["gpu:0:fan0"] } });
    }
    if (method === "GET" && url.includes("/fans")) {
      return route.fulfill({ json: { ...payload, ...control } });
    }
    return route.fulfill({ json: { ok: true } });
  });
}

const BOTH_CONTROLLABLE = [
  { ...CHANNELS[0] },
  {
    ...CHANNELS[1],
    controllable: true,
    control_reason: null,
    role: "case",
  },
];

test.describe("Настройки → Охлаждение", () => {
  test("пресеты выбираются отдельно для видеокарты и для платы", async ({
    page,
    context,
  }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    const bodies: Record<string, unknown>[] = [];
    await mockCooling(
      page,
      fansPayload({ hwmon_allowed: true, channels: BOTH_CONTROLLABLE }),
      seen,
    );
    page.on("request", (r) => {
      if (r.url().includes("/presets/") && r.method() === "POST") {
        bodies.push(r.postDataJSON() as Record<string, unknown>);
      }
    });
    await page.goto("/settings/cooling");

    const gpuRow = page.locator("div", { hasText: /^видеокарта · 1 кан\./ }).last();
    const boardRow = page
      .locator("div", { hasText: /^процессор и корпус · 1 кан\./ })
      .last();
    await expect(gpuRow).toBeVisible();
    await expect(boardRow).toBeVisible();

    await boardRow.getByRole("button", { name: "Тихий" }).click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toEqual({ scope: "mobo" });

    await gpuRow.getByRole("button", { name: "Максимум" }).click();
    await expect.poll(() => bodies.length).toBe(2);
    expect(bodies[1]).toEqual({ scope: "gpu" });
  });

  test("показывает каналы и объясняет, почему плата не управляется", async ({
    page,
    context,
  }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload(), seen);
    await page.goto("/settings/cooling");

    await expect(
      page.getByRole("heading", { name: "Охлаждение и вентиляторы" }),
    ).toBeVisible();
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
    await expect(
      page.getByText("Проверка выполнена — железо не тронуто."),
    ).toBeVisible();
    expect(seen.some((c) => c.startsWith("POST /fans/preview"))).toBe(true);
    expect(seen.some((c) => c.startsWith("POST /fans/config"))).toBe(false);
    expect(seen.some((c) => c.startsWith("POST /fans/manual"))).toBe(false);
  });

  test("возврат прошивке доступен всегда", async ({ page, context }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload({ control_enabled: false }), seen);
    await page.goto("/settings/cooling");

    await expect(
      page.getByText(/Управление выключено — показываются только обороты/),
    ).toBeVisible();
    const revert = page.getByRole("button", { name: "Вернуть всё прошивке" });
    await expect(revert).toBeEnabled();
    await revert.click();
    await expect(
      page.getByText(/возвращены под управление прошивки/),
    ).toBeVisible();
  });

  test("управление включается прямо со страницы, без правки .env", async ({
    page,
    context,
  }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(
      page,
      fansPayload({ control_enabled: false, hwmon_allowed: false }),
      seen,
    );
    await page.goto("/settings/cooling");

    const control = page.getByRole("checkbox", {
      name: /Разрешить управление оборотами/,
    });
    const board = page.getByRole("checkbox", {
      name: /вентиляторы материнской платы/,
    });
    await expect(control).not.toBeChecked();
    await expect(board).not.toBeChecked();

    // click(), not check(): the box is bound to server state, so it only flips
    // once the round-trip comes back — check() gives up before that.
    await control.click();
    await expect(control).toBeChecked();
    await expect
      .poll(() => seen.filter((c) => c === "POST /control").length)
      .toBe(1);
    await board.click();
    await expect(board).toBeChecked();
    await expect
      .poll(() => seen.filter((c) => c === "POST /control").length)
      .toBe(2);
  });

  test("инструкция открывается на странице и подтягивает текст из репозитория", async ({
    page,
    context,
  }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload(), seen);
    await page.goto("/settings/cooling");

    // Not fetched until it is actually opened.
    expect(seen.some((c) => c.includes("/setup-guide"))).toBe(false);
    await page
      .getByRole("button", { name: /Инструкция: как включить/ })
      .click();
    await expect(
      page.getByRole("heading", { name: "Шаг 1. Диагностика" }),
    ).toBeVisible();
    await expect(page.getByText(/fan-control-setup\.sh/).first()).toBeVisible();
  });

  test("аварийный обгон виден отдельным баннером", async ({
    page,
    context,
  }) => {
    await setAuthCookie(context);
    const seen: string[] = [];
    await mockCooling(page, fansPayload({ emergency: { gpu: 42 } }), seen);
    await page.goto("/settings/cooling");

    await expect(page.getByText("Аварийный обгон активен.")).toBeVisible();
    await expect(page.getByText(/GPU: ещё 42 с/)).toBeVisible();
  });
});
