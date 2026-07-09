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

const doneGeneration = {
  id: "22222222-2222-4222-8222-222222222222",
  job_id: null,
  operation: "edit",
  status: "done",
  progress: null,
  prompt: "чёрно-белый чертёж кронштейна с двумя отверстиями",
  negative_prompt: null,
  params: {},
  source_image_paths: [],
  mask_path: null,
  has_result: true,
  error: null,
  parent_id: null,
  accepted: false,
  workflow_id: null,
  created_at: "2026-07-07T12:00:00Z",
  source_document_id: null,
  case_id: null,
};

const customWorkflow = {
  id: "33333333-3333-4333-8333-333333333333",
  key: "custom_edit_wf",
  title: "Мой edit-воркфлоу",
  description: "",
  category: "edit",
  operation: "edit",
  graph: { "1": { class_type: "CLIPTextEncode", inputs: { text: "" } } },
  inject_map: { prompt: { node: "1", input: "text" } },
  params_schema: {},
  enabled: true,
  is_builtin: false,
  owner_sub: "e2e",
};

async function mockStudioApi(page: Page, patchCapture: { body: unknown }[]) {
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
      return route.fulfill({ json: { items: [doneGeneration] } });
    }
    if (url.pathname === "/api/studio/queue" && request.method() === "GET") {
      return route.fulfill({ json: { items: [] } });
    }
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET") {
      return route.fulfill({ json: [] });
    }
    if (url.pathname === "/api/image-gen/workflows/list") {
      return route.fulfill({ json: { items: [customWorkflow] } });
    }
    if (
      url.pathname === `/api/image-gen/workflows/${customWorkflow.id}` &&
      request.method() === "PATCH"
    ) {
      patchCapture.push({ body: request.postDataJSON() });
      return route.fulfill({
        json: { ...customWorkflow, ...(request.postDataJSON() as object) },
      });
    }
    if (url.pathname === "/api/studio/queue/stats") {
      return route.fulfill({
        json: {
          control: {
            paused: false,
            drain: false,
            reason: null,
            updated_at: null,
            updated_by: null,
          },
          limits: {
            global_active: 30,
            per_user_active: 4,
            operator_active: 12,
          },
          totals: { queued: 0 },
          active: 0,
          by_resource: {},
          by_kind: {},
          avg_wait_seconds_24h: 0,
          avg_runtime_seconds_24h: 0,
        },
      });
    }
    if (url.pathname === "/api/lora/gpu-status") {
      return route.fulfill({ json: { training_lock: null } });
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
    if (
      url.pathname === "/api/image-gen/generate" &&
      request.method() === "POST"
    ) {
      // Should never be reached in the empty-prompt test — client-side
      // validation must block the request before it gets here.
      return route.fulfill({
        status: 500,
        json: { detail: "should not be called" },
      });
    }
    // Generic fallback for anything else the app polls in the background
    // (chat sessions, notifications, etc.) — shaped as an empty list so
    // components that do `.map`/`.some` on the response don't crash.
    return route.fulfill({ json: { items: [] } });
  });
}

test("edit operation blocks submit with an empty prompt (client-side)", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockStudioApi(page, []);

  await page.goto("/studio");
  await page
    .getByRole("button", { name: "Редактировать", exact: true })
    .first()
    .click();

  // Pick the existing "done" generation as the source image.
  await page
    .getByRole("button", { name: "Выбрать из сгенерированных" })
    .click();
  await page.getByRole("button", { name: /Использовать/ }).click();

  // Prompt left blank -> submit must show the client-side error and never
  // call /api/image-gen/generate (mocked to fail loudly if it is).
  await page
    .getByRole("button", { name: "Сгенерировать", exact: true })
    .click();
  await expect(
    page.getByText("Опишите, что нужно сгенерировать."),
  ).toBeVisible();
});

test("generation gallery shows the prompt text, not an opaque id", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockStudioApi(page, []);

  await page.goto("/studio");
  await page
    .getByRole("button", { name: "Редактировать", exact: true })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Выбрать из сгенерированных" })
    .click();

  await expect(
    page.getByText("чёрно-белый чертёж кронштейна с двумя отверстиями").first(),
  ).toBeVisible();
  await expect(page.getByText(/^ID [0-9a-f]{8}$/)).toHaveCount(0);
});

test("workflow edit dialog saves the typed category, not a silently-derived one", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const patches: { body: unknown }[] = [];
  await mockStudioApi(page, patches);

  await page.goto("/studio");
  await page.getByRole("button", { name: "Воркфлоу", exact: true }).click();
  const workflowButton = page.getByRole("button", {
    name: customWorkflow.title,
    exact: true,
  });
  await workflowButton.waitFor({ state: "visible" });
  await workflowButton.click();
  await page
    .getByRole("button", { name: "Редактировать", exact: true })
    .click();

  // The label isn't programmatically associated (no htmlFor/id), so target
  // the input right after the "Категория" label text instead of getByLabel.
  const categoryInput = page.locator('label:text-is("Категория") + input');
  await categoryInput.fill("моя спецкатегория");
  await page.getByRole("button", { name: "Сохранить", exact: true }).click();

  await expect.poll(() => patches.length).toBeGreaterThan(0);
  expect((patches[0].body as { category?: string }).category).toBe(
    "моя спецкатегория",
  );
});
