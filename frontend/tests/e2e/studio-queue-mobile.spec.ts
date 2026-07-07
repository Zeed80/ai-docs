import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

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

const generation = {
  id: "11111111-1111-4111-8111-111111111111",
  job_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  operation: "generate",
  status: "queued",
  progress: { value: 15, max: 100, pct: 15, node: "KSampler", ts: 1 },
  prompt: "мобильный тест очереди",
  negative_prompt: null,
  params: {},
  source_image_paths: [],
  mask_path: null,
  has_result: false,
  error: null,
  parent_id: null,
  accepted: false,
  workflow_id: null,
  created_at: "2026-07-07T12:00:00Z",
  source_document_id: null,
  case_id: null,
};

const job = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  kind: "image_generation",
  status: "queued",
  resource: "comfyui",
  title: "мобильный тест очереди",
  priority: 0,
  position: 1,
  eta_seconds: 180,
  owner_sub: "e2e",
  created_at: "2026-07-07T12:00:00Z",
  queued_at: "2026-07-07T12:00:00Z",
  started_at: null,
  finished_at: null,
  cancel_requested_at: null,
  generation_id: generation.id,
  lora_run_id: null,
  linked_status: "queued",
  progress: generation.progress,
  error: null,
  can_cancel: true,
  can_retry: false,
  meta: {},
};

async function mockStudioApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/auth/me") {
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E User",
          preferred_username: "e2e",
          roles: ["admin"],
          groups: [],
        },
      });
    }
    if (url.pathname === "/api/image-gen" && request.method() === "GET") {
      return route.fulfill({ json: { items: [generation] } });
    }
    if (url.pathname === `/api/image-gen/${generation.id}`) {
      return route.fulfill({ json: generation });
    }
    if (url.pathname === "/api/studio/queue") {
      return route.fulfill({ json: { items: [job] } });
    }
    if (url.pathname === "/api/studio/queue/stats") {
      return route.fulfill({
        json: {
          control: { paused: false, drain: false, reason: null, updated_at: null, updated_by: null },
          limits: { global_active: 30, per_user_active: 4, operator_active: 12 },
          totals: { queued: 1 },
          active: 1,
          by_resource: { comfyui: { queued: 1 } },
          by_kind: { image_generation: { queued: 1 } },
          avg_wait_seconds_24h: 12,
          avg_runtime_seconds_24h: 118,
        },
      });
    }
    if (url.pathname === "/api/lora/gpu-status") {
      return route.fulfill({ json: { training_lock: null } });
    }
    if (url.pathname.includes("/priority") || url.pathname.includes("/cancel")) {
      return route.fulfill({ json: job });
    }
    if (url.pathname === "/api/studio/queue/control") {
      return route.fulfill({
        json: { paused: true, drain: false, reason: "E2E", updated_at: "2026-07-07T12:00:00Z", updated_by: "e2e" },
      });
    }
    if (url.pathname === "/api/studio/queue/bulk-cancel") {
      return route.fulfill({ json: { cancelled: 1 } });
    }
    if (url.pathname === "/api/notifications/unread-count") {
      return route.fulfill({ json: { count: 0 } });
    }
    if (url.pathname === "/api/dashboard/feed") {
      return route.fulfill({ json: { total: 0, items: [] } });
    }
    if (url.pathname === "/api/quarantine/count") {
      return route.fulfill({ json: { count: 0 } });
    }
    return route.fulfill({ json: {} });
  });
}

test("studio queue works on mobile viewport", async ({ page, context }) => {
  await setAuthCookie(context);
  await mockStudioApi(page);
  await page.setViewportSize({ width: 390, height: 844 });

  await page.goto("/studio");
  await page.getByRole("button", { name: "Очередь" }).click();

  await expect(page.getByText("Очередь задач").first()).toBeVisible();
  await expect(page.getByText("1/30").first()).toBeVisible();
  await expect(page.getByText("мобильный тест очереди").first()).toBeVisible();
  await expect(page.getByText("ETA 3m").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Пауза" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Отменить pending" })).toBeVisible();

  await page.locator("select").first().selectOption("queued,waiting_resource,running,cancel_requested");
  await page.getByRole("button", { name: "OK", exact: true }).click();
  await page.getByRole("button", { name: "Отменить" }).click();
});
