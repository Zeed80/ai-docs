/**
 * Вход через сайт облачного провайдера («Модели → Провайдеры»).
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test provider-oauth --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

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

function instance(kind: string, over: Record<string, unknown> = {}) {
  return {
    id: `${kind}-1`,
    kind,
    name: `${kind} (default)`,
    base_url: null,
    default_base_url: "",
    enabled: true,
    is_local: false,
    api_key_set: false,
    api_key_mask: "",
    api_key_state: "unset",
    extra: {},
    last_check_at: null,
    last_check_ok: null,
    last_error: null,
    auth_mode: "api_key",
    oauth: {},
    ...over,
  };
}

const KNOWN = [
  {
    kind: "moonshot",
    is_local: false,
    default_base_url: "",
    requires_api_key: true,
    oauth: {
      label: "аккаунт Kimi (Kimi Code)",
      note: "по подписке",
      flow: "device",
      regions: [],
    },
  },
  {
    kind: "openrouter",
    is_local: false,
    default_base_url: "",
    requires_api_key: true,
    oauth: {
      label: "аккаунт OpenRouter",
      note: "ключ аккаунта",
      flow: "redirect",
      regions: [],
    },
  },
  {
    kind: "deepseek",
    is_local: false,
    default_base_url: "",
    requires_api_key: true,
    oauth: null,
  },
];

async function setup(
  page: Page,
  state: { signedIn: boolean; polls: number; calls: string[] },
) {
  await mockEmptyApi(page);
  await page.route("**/api/local-models/status", (r) =>
    r.fulfill({ json: ALL_STATUS }),
  );
  await page.route("**/api/local-models/strata**", (r) =>
    r.fulfill({ status: 503, json: { detail: "Strata не настроена" } }),
  );
  await page.route("**/api/providers**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    state.calls.push(`${route.request().method()} ${path}`);
    if (path === "/api/providers" || path === "/api/providers/") {
      return route.fulfill({
        json: {
          instances: [
            instance("moonshot", state.signedIn
              ? { auth_mode: "oauth", oauth: { account: "me@kimi.com", expires_at: 1893456000, refreshable: true } }
              : {}), // prettier-ignore
            instance("openrouter"),
            instance("deepseek"),
          ],
          known_kinds: KNOWN,
        },
      });
    }
    if (path === "/api/providers/moonshot-1/oauth/start") {
      return route.fulfill({
        json: {
          flow_id: "f1",
          type: "device",
          user_code: "ABCD-1234",
          verification_uri:
            "https://www.kimi.com/code/authorize?user_code=ABCD-1234",
          expires_at: 1893456000,
          interval: 2,
        },
      });
    }
    if (path === "/api/providers/oauth/flows/f1/poll") {
      state.polls += 1;
      if (state.polls < 2)
        return route.fulfill({ json: { status: "pending" } });
      state.signedIn = true;
      return route.fulfill({ json: { status: "success" } });
    }
    if (path === "/api/providers/moonshot-1/test")
      return route.fulfill({ json: { ok: true, model_count: 1 } });
    if (path === "/api/providers/moonshot-1/refresh-models")
      return route.fulfill({
        json: { ok: true, added: ["moonshot_kimi-for-coding"], count: 1 },
      });
    if (path === "/api/providers/openrouter-1/oauth/start") {
      return route.fulfill({
        json: {
          flow_id: "or1",
          type: "redirect",
          authorize_url:
            "https://openrouter.ai/auth?callback_url=x&code_challenge=y",
        },
      });
    }
    return route.fulfill({ json: [] });
  });
}

test("device sign-in shows the code, waits and loads the models", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const state = { signedIn: false, polls: 0, calls: [] as string[] };
  await setup(page, state);
  await page.goto("/settings/models?tab=overview");

  await page
    .getByRole("button", { name: /Moonshot|Kimi/ })
    .first()
    .click();
  await expect(
    page.getByText("Вход через сайт — аккаунт Kimi (Kimi Code)"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Войти через сайт" }).click();

  await expect(page.getByLabel("Код подтверждения")).toHaveText("ABCD-1234");
  const link = page.getByRole("link", { name: "Открыть страницу входа" });
  await expect(link).toHaveAttribute("href", /kimi\.com\/code\/authorize/);
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(
    page.getByText("Ждём подтверждения на сайте провайдера…"),
  ).toBeVisible();

  await expect(page.getByText("Вход выполнен: me@kimi.com")).toBeVisible({
    timeout: 15000,
  });
  expect(state.calls).toContain("POST /api/providers/moonshot-1/test");
  expect(state.calls).toContain(
    "POST /api/providers/moonshot-1/refresh-models",
  );
  const block = page
    .getByText("Вход через сайт — аккаунт Kimi (Kimi Code)")
    .locator("..");
  await expect(block.getByRole("button", { name: "Выйти" })).toBeVisible();
});

test("a provider without site sign-in shows only the key", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await setup(page, { signedIn: false, polls: 0, calls: [] });
  await page.goto("/settings/models?tab=overview");
  await page
    .getByRole("button", { name: /DeepSeek/ })
    .first()
    .click();
  await expect(
    page.getByRole("button", { name: "Войти через сайт" }),
  ).toHaveCount(0);
  await expect(page.getByText("API-ключ", { exact: true })).toBeVisible();
});

test("OpenRouter sign-in goes to its site and the return is announced", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await setup(page, { signedIn: false, polls: 0, calls: [] });
  await page.route("https://openrouter.ai/**", (r) =>
    r.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<h1>OpenRouter login</h1>",
    }),
  );
  await page.goto("/settings/models?tab=overview");
  await page
    .getByRole("button", { name: /OpenRouter/ })
    .first()
    .click();
  await page.getByRole("button", { name: "Войти через сайт" }).click();
  await page.waitForURL(/openrouter\.ai\/auth/);

  await page.goto("/settings/models?tab=overview&oauth=openrouter&oauth_ok=1");
  await expect(page.getByText("OpenRouter подключён")).toBeVisible();
  await expect(page).toHaveURL(/tab=overview$/);
});
