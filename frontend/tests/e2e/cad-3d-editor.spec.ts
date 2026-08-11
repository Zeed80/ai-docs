import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

const generationId = "44444444-4444-4444-8444-444444444444";
let compilePayload: Record<string, unknown> | null = null;
let specCorrectionPayload: Record<string, unknown> | null = null;

const generation = {
  id: generationId,
  job_id: null,
  operation: "vectorize",
  status: "done",
  progress: null,
  prompt: "CAD 3D E2E",
  negative_prompt: null,
  params: {
    full_check_revision: 3,
    cad_artifact_revision: 3,
    cad_candidate_index: 0,
    spec: {
      main_view: {
        outer: [{ diameter_mm: 80, length_mm: 100 }],
        axial_holes: [
          {
            count: 2,
            bolt_circle_diameter_mm: 65,
            from_face: null,
            through: null,
            depth_mm: null,
            thread_depth_mm: null,
            drill_depth_mm: null,
            pilot_diameter_mm: null,
            thread: { designation: "M8", nominal_diameter_mm: 8 },
          },
        ],
        chamfers: [{ size_mm: 1, angle_deg: 45, location: "left_end" }],
      },
      dimensions: [{ value: "6 фасок 1×45°" }],
      unresolved: [
        "малые элементы: осевые отверстия M8: не определены торец",
        "малые элементы: указано 6 фасок, локализовано 1",
      ],
    },
    cad_report: {
      valid: true,
      solid_count: 1,
      volume_mm3: 118429.2,
      bounds_mm: { x: 100, y: 60, z: 20 },
      warnings: [],
      edges: [
        {
          key: "edge-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          index: 1,
          curve: "Line",
          length_mm: 100,
          vertices: [
            { x: 0, y: 0, z: 0 },
            { x: 100, y: 0, z: 0 },
          ],
        },
      ],
      // Ф3.1/3.2: matches topologyMesh below — one entry per box face.
      faces: [
        {
          key: "face-bottom",
          index: 1,
          surface: "Plane",
          area_mm2: 6000,
          center_of_mass_mm: { x: 0, y: 0, z: -10 },
        },
        {
          key: "face-top",
          index: 2,
          surface: "Plane",
          area_mm2: 6000,
          center_of_mass_mm: { x: 0, y: 0, z: 10 },
        },
        {
          key: "face-front",
          index: 3,
          surface: "Plane",
          area_mm2: 2000,
          center_of_mass_mm: { x: 0, y: -30, z: 0 },
        },
        {
          key: "face-back",
          index: 4,
          surface: "Plane",
          area_mm2: 2000,
          center_of_mass_mm: { x: 0, y: 30, z: 0 },
        },
        {
          key: "face-right",
          index: 5,
          surface: "Plane",
          area_mm2: 1200,
          center_of_mass_mm: { x: 50, y: 0, z: 0 },
        },
        {
          key: "face-left",
          index: 6,
          surface: "Plane",
          area_mm2: 1200,
          center_of_mass_mm: { x: -50, y: 0, z: 0 },
        },
      ],
    },
  },
  source_image_paths: [],
  mask_path: null,
  has_result: true,
  error: null,
  parent_id: null,
  accepted: true,
  accepted_by: "e2e",
  accepted_revision: 3,
  workflow_id: null,
  created_at: "2026-07-11T12:00:00Z",
  source_document_id: null,
  case_id: null,
};

const ir = {
  schema_version: "2.0",
  source: { image_width: 400, image_height: 240, kind: "blank" },
  scale: 0.25,
  scale_source: "sheet_format",
  sheet: { format: "A4", frame: false, title_block: {} },
  entities: [
    {
      id: "s1",
      type: "segment",
      p1: { x: 0, y: 0 },
      p2: { x: 400, y: 0 },
      line_class: "contour",
      width_class: "main",
      confidence: 1,
      origin: "human",
      assurance: "human_approved",
    },
    {
      id: "s2",
      type: "segment",
      p1: { x: 400, y: 0 },
      p2: { x: 400, y: 240 },
      line_class: "contour",
      width_class: "main",
      confidence: 1,
      origin: "human",
      assurance: "human_approved",
    },
    {
      id: "s3",
      type: "segment",
      p1: { x: 400, y: 240 },
      p2: { x: 0, y: 240 },
      line_class: "contour",
      width_class: "main",
      confidence: 1,
      origin: "human",
      assurance: "human_approved",
    },
    {
      id: "s4",
      type: "segment",
      p1: { x: 0, y: 240 },
      p2: { x: 0, y: 0 },
      line_class: "contour",
      width_class: "main",
      confidence: 1,
      origin: "human",
      assurance: "human_approved",
    },
    {
      id: "c1",
      type: "circle",
      center: { x: 120, y: 120 },
      radius: 20,
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

const asciiStl = `solid bracket
facet normal 0 0 -1
 outer loop
  vertex -50 -30 -10
  vertex 50 -30 -10
  vertex 50 30 -10
 endloop
endfacet
facet normal 0 0 1
 outer loop
  vertex -50 -30 10
  vertex 50 30 10
  vertex 50 -30 10
 endloop
endfacet
facet normal 0 -1 0
 outer loop
  vertex -50 -30 -10
  vertex -50 -30 10
  vertex 50 -30 10
 endloop
endfacet
facet normal 1 0 0
 outer loop
  vertex 50 -30 -10
  vertex 50 -30 10
  vertex 50 30 10
 endloop
endfacet
facet normal 0 1 0
 outer loop
  vertex 50 30 -10
  vertex 50 30 10
  vertex -50 30 10
 endloop
endfacet
facet normal -1 0 0
 outer loop
  vertex -50 30 -10
  vertex -50 30 10
  vertex -50 -30 10
 endloop
endfacet
facet normal 0 0 -1
 outer loop
  vertex -50 -30 -10
  vertex 50 30 -10
  vertex -50 30 -10
 endloop
endfacet
facet normal 0 0 1
 outer loop
  vertex -50 -30 10
  vertex -50 30 10
  vertex 50 30 10
 endloop
endfacet
facet normal 0 -1 0
 outer loop
  vertex -50 -30 -10
  vertex 50 -30 10
  vertex 50 -30 -10
 endloop
endfacet
facet normal 1 0 0
 outer loop
  vertex 50 -30 -10
  vertex 50 30 10
  vertex 50 30 -10
 endloop
endfacet
facet normal 0 1 0
 outer loop
  vertex 50 30 -10
  vertex -50 30 10
  vertex -50 30 -10
 endloop
endfacet
facet normal -1 0 0
 outer loop
  vertex -50 30 -10
  vertex -50 -30 10
  vertex -50 -30 -10
 endloop
endfacet
endsolid bracket`;

// Ф3.1/3.2: one quad (2 triangles) per box face — same ±50/±30/±10 box the
// ASCII STL above draws, split by face instead of by facet pair, keyed to
// match cad_report.faces above.
function quad(corners: [number, number, number][]): {
  vertices: number[][];
  triangles: number[][];
} {
  return {
    vertices: corners,
    triangles: [
      [0, 1, 2],
      [0, 2, 3],
    ],
  };
}
const topologyMesh = {
  schema: "cad-kernel-topology/1.0",
  faces: [
    {
      key: "face-bottom",
      ...quad([
        [-50, -30, -10],
        [50, -30, -10],
        [50, 30, -10],
        [-50, 30, -10],
      ]),
    },
    {
      key: "face-top",
      ...quad([
        [-50, -30, 10],
        [50, -30, 10],
        [50, 30, 10],
        [-50, 30, 10],
      ]),
    },
    {
      key: "face-front",
      ...quad([
        [-50, -30, -10],
        [50, -30, -10],
        [50, -30, 10],
        [-50, -30, 10],
      ]),
    },
    {
      key: "face-back",
      ...quad([
        [-50, 30, -10],
        [50, 30, -10],
        [50, 30, 10],
        [-50, 30, 10],
      ]),
    },
    {
      key: "face-right",
      ...quad([
        [50, -30, -10],
        [50, 30, -10],
        [50, 30, 10],
        [50, -30, 10],
      ]),
    },
    {
      key: "face-left",
      ...quad([
        [-50, -30, -10],
        [-50, 30, -10],
        [-50, 30, 10],
        [-50, -30, 10],
      ]),
    },
  ],
};

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
    if (url.pathname === "/api/image-gen" && request.method() === "GET") {
      return route.fulfill({ json: { items: [generation] } });
    }
    if (url.pathname === `/api/image-gen/${generationId}`)
      return route.fulfill({ json: generation });
    if (
      url.pathname === `/api/image-gen/${generationId}/spec-correction` &&
      request.method() === "POST"
    ) {
      specCorrectionPayload = request.postDataJSON() as Record<string, unknown>;
      return route.fulfill({
        json: { ok: true, diff: {}, rebuild_task_id: null },
      });
    }
    if (url.pathname === `/api/image-gen/${generationId}/ir`) {
      return route.fulfill({
        json: { revision: 3, origin: "editor", summary: {}, ir },
      });
    }
    if (url.pathname.endsWith("/ir/feature-tree-candidates")) {
      return route.fulfill({
        json: {
          candidates: [
            {
              features: [
                {
                  kind: "extrude",
                  source_entity_ids: ["s1", "s2", "s3", "s4"],
                  params: { width_mm: 100, height_mm: 60, depth_mm: 20 },
                  confidence: 0.2,
                },
                {
                  kind: "hole",
                  source_entity_ids: ["c1"],
                  params: {
                    diameter_mm: 10,
                    center_x_mm: 30,
                    center_y_mm: 30,
                    through: null,
                  },
                  confidence: 0.5,
                },
              ],
              score: 0.2,
              label: "Глубина 20 мм",
              missing_data: [
                "нет бокового вида/разреза — глубина выдавливания не измерена, это эвристика",
                "глубина отверстия 10мм не указана на чертеже (сквозное/глухое)",
              ],
            },
          ],
        },
      });
    }
    if (url.pathname.endsWith("/step") && request.method() === "POST") {
      compilePayload = request.postDataJSON() as Record<string, unknown>;
      return route.fulfill({ body: "ISO-10303-21", contentType: "model/step" });
    }
    if (
      url.pathname.endsWith("/artifact") &&
      url.searchParams.get("kind") === "stl"
    ) {
      return route.fulfill({ body: asciiStl, contentType: "model/stl" });
    }
    if (
      url.pathname.endsWith("/artifact") &&
      url.searchParams.get("kind") === "topology"
    ) {
      return route.fulfill({ json: topologyMesh });
    }
    if (url.pathname.endsWith("/result")) {
      return route.fulfill({
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAEAQH/6B9pAAAAAElFTkSuQmCC",
          "base64",
        ),
        contentType: "image/png",
      });
    }
    if (url.pathname.endsWith("/release-manifest")) {
      return route.fulfill({
        json: {
          manifest_version: "1",
          generation_id: generationId,
          revision: 3,
          dxf_version: "R2010",
          manifest_sha256: "a".repeat(64),
          artifact_hashes: {},
          fully_reproducible: true,
          approval: { accepted_by: "e2e", accepted_at: "2026-07-17T10:00:00Z" },
        },
      });
    }
    if (url.pathname === "/api/studio/queue")
      return route.fulfill({ json: { items: [] } });
    if (url.pathname === "/api/studio/queue/stats")
      return route.fulfill({
        json: {
          control: { paused: false, drain: false },
          limits: {},
          totals: {},
          active: 0,
          by_resource: {},
          by_kind: {},
        },
      });
    if (url.pathname === "/api/lora/gpu-status")
      return route.fulfill({ json: { training_lock: null } });
    if (url.pathname === "/api/image-gen/workflows/list")
      return route.fulfill({ json: { items: [] } });
    if (url.pathname === "/api/chat/sessions")
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/dashboard/feed")
      return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    return route.fulfill({ json: { items: [] } });
  });
}

test("CAD blocker editor saves explicit M8 fields without guessing chamfers", async ({
  page,
  context,
}) => {
  specCorrectionPayload = null;
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/cad/${generationId}`);

  await expect(
    page.getByRole("button", {
      name: "Прочитанная спецификация (можно исправить)",
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Готовность полной модели: 2 из 4"),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Осевые резьбовые отверстия" }),
  ).toBeVisible();
  await expect(page.getByText("Локализовано 1 из 6")).toBeVisible();
  await expect(page.getByText(/Геометрия из стандарта/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Сохранить и обновить 3D-черновик" }),
  ).toBeDisabled();
  await page
    .getByRole("heading", { name: "Осевые резьбовые отверстия" })
    .locator("xpath=ancestor::section")
    .screenshot({ path: "../test-results/cad-blocker-editor.png" });

  await page.getByLabel("С какого торца").selectOption("zmax");
  await page.getByLabel("Исполнение").selectOption("blind");
  await page.getByLabel("Глубина резьбы, мм").fill("15");
  await page.getByLabel("Глубина сверления, мм").fill("17");
  await expect(
    page.getByRole("button", { name: "Сохранить и обновить 3D-черновик" }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Только сохранить исправление" })
    .click();

  await expect.poll(() => specCorrectionPayload).not.toBeNull();
  expect(specCorrectionPayload).toEqual({
    axial_holes: [
      {
        count: 2,
        bolt_circle_diameter_mm: 65,
        from_face: "zmax",
        through: false,
        depth_mm: null,
        thread_depth_mm: 15,
        drill_depth_mm: 17,
        pilot_diameter_mm: null,
        thread: { designation: "M8", nominal_diameter_mm: 8 },
      },
    ],
    rebuild: false,
  });
});

test("CAD feature tree rebuild sends only editable 3D parameters", async ({
  page,
  context,
}) => {
  compilePayload = null;
  await setAuthCookie(context);
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/cad/${generationId}`);

  const depth = page.getByLabel("Глубина выдавливания");
  await expect(depth).toHaveValue("20");
  await depth.fill("24");
  await page.getByLabel("Тип отверстия").selectOption("blind");
  await page.getByLabel("Глубина глухого отверстия").fill("8");
  await page.getByRole("button", { name: "+ Добавить бобышку" }).click();
  await expect(page.getByText("3. Бобышка")).toBeVisible();
  await page.getByRole("button", { name: "+ Добавить скругление" }).click();
  await expect(page.getByText("4. Скругление ребра")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.getByTestId("cad-feature-tree").screenshot({
    path: "../test-results/cad-feature-tree-mobile.png",
  });

  await expect(
    page.getByText("Параметры изменены — перестройте 3D-модель"),
  ).toBeVisible();
  await expect(page.locator('canvas[data-testid="cad-3d-canvas"]')).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Построить 3D" }).click();
  await assertCanvasHasModel(page);

  expect(compilePayload).toEqual({
    confirm_assumptions: false,
    feature_overrides: [
      { feature_index: 0, depth_mm: 24 },
      { feature_index: 1, through: false, depth_mm: 8 },
    ],
    added_features: [
      {
        kind: "boss",
        profile: "circle",
        center_x_mm: 50,
        center_y_mm: 30,
        depth_mm: 6,
        diameter_mm: 15,
      },
      {
        kind: "fillet",
        edge_key:
          "edge-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        size_mm: 1,
      },
    ],
  });
});

async function assertCanvasHasModel(page: Page) {
  const canvas = page.locator('canvas[data-testid="cad-3d-canvas"]');
  await expect(canvas).toBeVisible();
  await expect(page.getByText("Загрузка 3D-модели…")).toHaveCount(0);
  const pixels = await canvas.evaluate((node) => {
    const element = node as HTMLCanvasElement;
    const gl = element.getContext("webgl2") ?? element.getContext("webgl");
    if (!gl) return { colored: 0, width: 0, height: 0 };
    const width = gl.drawingBufferWidth;
    const height = gl.drawingBufferHeight;
    const data = new Uint8Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, data);
    let colored = 0;
    for (let i = 0; i < data.length; i += 16) {
      if (data[i] > 70 || data[i + 1] > 70 || data[i + 2] > 70) colored += 1;
    }
    return { colored, width, height };
  });
  expect(pixels.width).toBeGreaterThan(100);
  expect(pixels.height).toBeGreaterThan(100);
  expect(pixels.colored).toBeGreaterThan(100);
}

for (const viewport of [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`CAD 3D preview renders without overlap on ${viewport.name}`, async ({
    page,
    context,
  }) => {
    await setAuthCookie(context);
    await mockApi(page);
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await page.goto(`/cad/${generationId}`);
    await expect(page.getByText("Параметрическая 3D-модель")).toBeVisible({
      timeout: 20_000,
    });
    await assertCanvasHasModel(page);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
    ).toBe(true);
    await page.locator('canvas[data-testid="cad-3d-canvas"]').screenshot({
      path: `../test-results/cad-3d-canvas-${viewport.name}.png`,
    });
    await page.screenshot({
      path: `../test-results/cad-3d-${viewport.name}.png`,
      fullPage: true,
    });
  });
}

test("clicking the 3D model selects a face and shows its info", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/cad/${generationId}`);
  await expect(page.getByText("Параметрическая 3D-модель")).toBeVisible({
    timeout: 20_000,
  });
  await assertCanvasHasModel(page);

  const canvas = page.locator('canvas[data-testid="cad-3d-canvas"]');
  // The topology fetch/swap is async and separate from the STL becoming
  // visible — wait for the per-face meshes to actually be live before
  // clicking, or the click would (correctly) hit nothing raycastable yet.
  await expect(canvas).toHaveAttribute("data-topology-ready", "true");
  // Center of the canvas (Playwright's default click target) — guaranteed
  // to land on the model regardless of which specific face the camera
  // angle happens to centre.
  await canvas.click();

  await expect(page.getByText(/Поверхность: Plane/)).toBeVisible();
  await expect(page.getByText(/Площадь: [\d.]+ мм²/)).toBeVisible();

  await page.getByRole("button", { name: "Снять выделение" }).click();
  await expect(page.getByText(/Поверхность: Plane/)).toHaveCount(0);
});
