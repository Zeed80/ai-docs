// H3: visual regression of the 2D CAD canvas — non-blank rendering, framing
// after `fit`, and the selection overlay, verified by PIXELS of a real
// screenshot (an SVG can be "attached" yet render nothing; only pixels prove
// the drawing is on screen). 3D viewport non-blank lives in cad-3d-editor.
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";
// @ts-expect-error — pngjs ships no type declarations
import { PNG } from "pngjs";

const generationId = "66666666-6666-4666-8666-666666666666";

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
        p1: { x: 40, y: 40 },
        p2: { x: 360, y: 40 },
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
      },
      {
        id: "s2",
        type: "segment",
        p1: { x: 360, y: 40 },
        p2: { x: 360, y: 200 },
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
      },
      {
        id: "s3",
        type: "segment",
        p1: { x: 360, y: 200 },
        p2: { x: 40, y: 200 },
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
      },
      {
        id: "s4",
        type: "segment",
        p1: { x: 40, y: 200 },
        p2: { x: 40, y: 40 },
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
      },
      {
        id: "c1",
        type: "circle",
        center: { x: 200, y: 120 },
        radius: 50,
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
      },
    ],
    validation: { issues: [], coverage_recall: 1, coverage_precision: 1 },
    review: [],
    parameters: [],
    constraints: [],
    recognizer_used: "manual",
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

async function mockApi(page: Page) {
  const ir = makeIr();
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
      return route.fulfill({
        json: {
          id: generationId,
          job_id: null,
          operation: "vectorize",
          status: "done",
          progress: null,
          prompt: "H3 visual",
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
        },
      });
    }
    if (
      url.pathname === `/api/image-gen/${generationId}/ir` &&
      request.method() === "GET"
    ) {
      return route.fulfill({
        json: { revision: 0, origin: "editor", summary: {}, ir },
      });
    }
    if (url.pathname.endsWith("/ir/constraints/evaluate")) {
      return route.fulfill({ json: { checks: [], violated: 0, dof: null } });
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
    if (url.pathname === "/api/chat/sessions") return route.fulfill({ json: [] });
    if (url.pathname === "/api/notifications/unread-count") return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/quarantine/count") return route.fulfill({ json: { count: 0 } });
    return route.fulfill({ json: { items: [] } });
  });
}

type PixelStats = {
  nonWhite: number;
  selection: number;
  bbox: { minX: number; maxX: number; minY: number; maxY: number } | null;
  width: number;
  height: number;
};

async function canvasPixels(page: Page): Promise<PixelStats> {
  const canvas = page.locator(".bg-white").first();
  await expect(canvas).toBeVisible();
  const buffer = await canvas.screenshot();
  const png = PNG.sync.read(buffer);
  let nonWhite = 0;
  let selection = 0;
  let minX = Infinity,
    maxX = -Infinity,
    minY = Infinity,
    maxY = -Infinity;
  for (let y = 0; y < png.height; y += 2) {
    for (let x = 0; x < png.width; x += 2) {
      const i = (png.width * y + x) * 4;
      const r = png.data[i],
        g = png.data[i + 1],
        b = png.data[i + 2];
      if (r < 235 || g < 235 || b < 235) {
        nonWhite += 1;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
      // selection stroke #38bdf8 = rgb(56, 189, 248)
      if (
        Math.abs(r - 56) < 30 &&
        Math.abs(g - 189) < 30 &&
        Math.abs(b - 248) < 30
      ) {
        selection += 1;
      }
    }
  }
  return {
    nonWhite,
    selection,
    bbox: nonWhite ? { minX, maxX, minY, maxY } : null,
    width: png.width,
    height: png.height,
  };
}

async function openEditor(page: Page) {
  await page.goto(`/cad/${generationId}`);
  await expect(page.locator(".bg-white svg line").first()).toBeAttached();
}

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`2D canvas renders NON-BLANK geometry on ${viewport.name}`, async ({
    page,
    context,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await setAuthCookie(context);
    await mockApi(page);
    await openEditor(page);

    const stats = await canvasPixels(page);
    expect(stats.width).toBeGreaterThan(100);
    // the rectangle + circle must produce a substantial amount of ink
    expect(stats.nonWhite).toBeGreaterThan(200);
  });
}

test("fit frames the whole drawing inside the canvas", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await openEditor(page);

  const cmd = page.getByPlaceholder(/команда или координаты/);
  await cmd.fill("fit");
  await cmd.press("Enter");

  const stats = await canvasPixels(page);
  expect(stats.bbox).not.toBeNull();
  const bbox = stats.bbox!;
  // framing: the geometry spans a healthy share of the canvas in both axes
  // and does not run off the edges
  expect(bbox.maxX - bbox.minX).toBeGreaterThan(stats.width * 0.4);
  expect(bbox.maxY - bbox.minY).toBeGreaterThan(stats.height * 0.4);
  expect(bbox.minX).toBeGreaterThanOrEqual(0);
  expect(bbox.maxX).toBeLessThan(stats.width);
});

test("selecting an entity paints the sky selection overlay", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await openEditor(page);

  const before = await canvasPixels(page);
  expect(before.selection).toBeLessThan(10);

  // click on the circle's stroke: centre (200,120) radius 50 → point (250,120)
  const canvas = page.locator(".bg-white").first();
  const box = (await canvas.boundingBox())!;
  await page.mouse.click(
    box.x + (250 / 400) * box.width,
    box.y + (120 / 240) * box.height,
  );

  await expect
    .poll(async () => (await canvasPixels(page)).selection)
    .toBeGreaterThan(10);
});
