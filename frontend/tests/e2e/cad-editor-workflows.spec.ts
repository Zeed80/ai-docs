// H2: CAD editor workflows — keyboard drafting, review acceptance,
// parameterization and the release gate, on desktop and mobile.
// Route-mocked (same pattern as cad-3d-editor.spec.ts): the spec drives the
// real editor UI and asserts the exact PATCH /ir payloads it emits.
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

const generationId = "55555555-5555-4555-8555-555555555555";

function makeIr() {
  return {
    schema_version: "2.0",
    source: { image_width: 400, image_height: 240, kind: "blank" },
    scale: 1,
    scale_source: "manual",
    sheet: { format: "A4", frame: false, title_block: {} },
    entities: [
      {
        id: "s1",
        type: "segment",
        p1: { x: 10, y: 10 },
        p2: { x: 200, y: 10 },
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
      },
      // low-confidence INFERRED text in the review queue → critical unresolved
      {
        id: "t1",
        type: "text",
        position: { x: 50, y: 50 },
        text: "M12x1.75",
        height: 12,
        line_class: "dim",
        width_class: "thin",
        confidence: 0.55,
        origin: "cv",
        assurance: "inferred",
      },
    ],
    validation: { issues: [], coverage_recall: 1, coverage_precision: 1 },
    review: [{ entity_id: "t1", reason: "low_confidence", resolved: false }],
    parameters: [],
    constraints: [],
    recognizer_used: "manual",
  };
}

function makeGeneration() {
  return {
    id: generationId,
    job_id: null,
    operation: "vectorize",
    status: "done",
    progress: null,
    prompt: "H2 workflows",
    negative_prompt: null,
    params: {},
    source_image_paths: [],
    mask_path: null,
    has_result: true,
    error: null,
    parent_id: null,
    accepted: false,
    accepted_by: null,
    accepted_revision: null,
    workflow_id: null,
    created_at: "2026-07-17T10:00:00Z",
    source_document_id: null,
    case_id: null,
  };
}

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

type Captured = { patches: Array<{ ops: Array<Record<string, unknown>> }> };

async function mockApi(
  page: Page,
  ir: ReturnType<typeof makeIr>,
): Promise<Captured> {
  const captured: Captured = { patches: [] };
  const generation = makeGeneration();
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/auth/me") {
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E",
          roles: ["admin"],
          groups: [],
        },
      });
    }
    if (
      url.pathname === `/api/image-gen/${generationId}` &&
      request.method() === "GET"
    ) {
      return route.fulfill({ json: generation });
    }
    if (
      url.pathname === `/api/image-gen/${generationId}/ir` &&
      request.method() === "GET"
    ) {
      return route.fulfill({
        json: {
          revision: captured.patches.length,
          origin: "editor",
          summary: {},
          ir,
        },
      });
    }
    if (
      url.pathname === `/api/image-gen/${generationId}/ir` &&
      request.method() === "PATCH"
    ) {
      const body = request.postDataJSON() as {
        ops: Array<Record<string, unknown>>;
      };
      captured.patches.push(body);
      // apply confirm/set_parameters minimally so the UI reflects the change
      for (const op of body.ops) {
        if (op.op === "confirm") {
          const entity = ir.entities.find((e) => e.id === op.entity_id);
          if (entity) entity.assurance = "human_approved";
          const item = ir.review.find((r) => r.entity_id === op.entity_id);
          if (item) item.resolved = true;
        }
        if (op.op === "add")
          ir.entities.push({
            id: `new${ir.entities.length}`,
            ...(op.entity as object),
          } as never);
        if (op.op === "set_parameters") ir.parameters = op.parameters as never;
      }
      return route.fulfill({
        json: {
          revision: captured.patches.length,
          origin: "editor",
          summary: {},
          ir,
        },
      });
    }
    if (url.pathname.endsWith("/ir/constraints/evaluate")) {
      return route.fulfill({ json: { checks: [], violated: 0, dof: null } });
    }
    if (url.pathname.endsWith("/revisions"))
      return route.fulfill({ json: { items: [] } });
    if (url.pathname.endsWith("/release-manifest")) {
      return route.fulfill({
        json: {
          manifest_version: "1",
          generation_id: generationId,
          revision: 1,
          dxf_version: "R2010",
          artifact_hashes: {},
          deterministic: true,
          approvals: [],
        },
      });
    }
    if (url.pathname.endsWith("/result") || url.pathname.endsWith("/source")) {
      return route.fulfill({
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAEAQH/6B9pAAAAAElFTkSuQmCC",
          "base64",
        ),
        contentType: "image/png",
      });
    }
    if (url.pathname === "/api/chat/sessions")
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    return route.fulfill({ json: { items: [] } });
  });
  return captured;
}

async function openEditor(page: Page) {
  await page.goto(`/cad/${generationId}`);
  await expect(page.locator("svg").first()).toBeVisible();
}

test("keyboard drafting: line command + coordinates add a segment via PATCH", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const captured = await mockApi(page, makeIr());
  await openEditor(page);

  const cmd = page.getByPlaceholder(/команда или координаты/);
  await cmd.fill("line");
  await cmd.press("Enter");
  await cmd.fill("100,50");
  await cmd.press("Enter");
  await cmd.fill("@50,0");
  await cmd.press("Enter");

  await expect
    .poll(
      () =>
        captured.patches.flatMap((p) => p.ops).filter((o) => o.op === "add")
          .length,
    )
    .toBeGreaterThan(0);
  const add = captured.patches
    .flatMap((p) => p.ops)
    .find((o) => o.op === "add") as {
    entity: {
      type: string;
      p1: { x: number; y: number };
      p2: { x: number; y: number };
    };
  };
  expect(add.entity.type).toBe("segment");
  expect(add.entity.p1).toEqual({ x: 100, y: 50 });
  expect(add.entity.p2).toEqual({ x: 150, y: 50 });
});

test("release gate: critical unresolved text blocks accept; confirming unblocks", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const captured = await mockApi(page, makeIr());
  await openEditor(page);

  // the inferred low-confidence text blocks acceptance
  await expect(page.getByText(/Неподтверждённых критических/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Принять чертёж" }),
  ).toBeDisabled();

  // review acceptance: confirm the pending item from the review queue
  await expect(page.getByText(/Спорные места/)).toBeVisible();
  await page.getByRole("button", { name: "✓" }).first().click();
  await expect
    .poll(
      () =>
        captured.patches.flatMap((p) => p.ops).filter((o) => o.op === "confirm")
          .length,
    )
    .toBe(1);

  // the critical gate lifts (the remaining accept gate is the opt-in full
  // check — a separate LLM stage, not this test's subject)
  await expect(page.getByText(/Неподтверждённых критических/)).toHaveCount(0);
  await expect(page.getByText(/Запустите полную проверку/)).toBeVisible();
});

test("parameterization: an expression parameter round-trips through set_parameters", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  const captured = await mockApi(page, makeIr());
  await openEditor(page);

  await page.getByPlaceholder("Имя").fill("width");
  await page.getByPlaceholder(/значение или/).fill("2*height+5");
  await page.getByRole("button", { name: "Сохранить" }).click();

  await expect
    .poll(
      () =>
        captured.patches
          .flatMap((p) => p.ops)
          .filter((o) => o.op === "set_parameters").length,
    )
    .toBe(1);
  const op = captured.patches
    .flatMap((p) => p.ops)
    .find((o) => o.op === "set_parameters") as {
    parameters: Array<{ name: string; expression: string | null }>;
  };
  expect(op.parameters[0].name).toBe("width");
  expect(op.parameters[0].expression).toBe("2*height+5");
});

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`editor renders canvas, toolbar and command line on ${viewport.name}`, async ({
    page,
    context,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await setAuthCookie(context);
    await mockApi(page, makeIr());
    await openEditor(page);

    await expect(page.getByRole("button", { name: "Линия", exact: true })).toBeVisible();
    await expect(page.getByPlaceholder(/команда или координаты/)).toBeVisible();
    // the drawing itself is on screen: the canvas svg is laid out and the
    // contour segment is attached (a horizontal line has a zero-height
    // bounding box, so toBeVisible() would false-negative on it)
    await expect(page.locator(".bg-white svg").first()).toBeVisible();
    await expect(page.locator(".bg-white svg line").first()).toBeAttached();
  });
}
