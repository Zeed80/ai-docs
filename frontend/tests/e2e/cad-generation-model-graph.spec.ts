import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

const generationId = "55555555-5555-4555-8555-555555555555";
let verifyCalls = 0;
let readerCalls = 0;
let correctionCalls = 0;
let batchCorrectionCalls = 0;

const editorGraph = {
  id: "66666666-6666-4666-8666-666666666666",
  generation_id: generationId,
  engineering_project_id: null,
  engineering_revision_id: null,
  graph_id: `image-generation:${generationId}`,
  revision: 0,
  parent_revision: null,
  canonical_sha256: "c".repeat(64),
  profile: "mechanical",
  source_generation_status: "failed",
  workflow_status: "review_required",
  comprehension_status: "accumulating",
  build_status: "not_ready",
  release_status: "blocked",
  graph: {
    nodes: [
      { id: "product:legacy-spec", type: "Product", name: "Проблемный вал" },
      { id: "region:hole", type: "SourceRegion", name: "Hole callout" },
      { id: "operation:outer", type: "BuildOperation", name: "Outer profile" },
      { id: "feature:outer", type: "Feature", name: "Outer cylinder" },
      { id: "topology:face:outer", type: "TopologyElement", name: "Outer face" },
      { id: "artifact:step", type: "Artifact", name: "STEP model" },
    ],
    edges: [
      { id: "realizes:outer", type: "realizes", source_id: "operation:outer", target_id: "feature:outer" },
      { id: "maps:outer", type: "maps_to_topology", source_id: "feature:outer", target_id: "topology:face:outer" },
    ],
    assertions: [{
      id: "assertion:hole-location",
      subject_id: "product:legacy-spec",
      predicate: "hole.location_mm",
      value: { kind: "unknown", reason: "not localized" },
      origin: "observed",
      assurance: "proposed",
      confidence: 0.4,
      impacts: ["connection_opening"],
      evidence_ids: ["evidence:whole-sheet"],
      state: "active",
    }, {
      id: "assertion:shaft-length",
      subject_id: "product:legacy-spec",
      predicate: "length_mm",
      value: { kind: "unknown", reason: "needs review" },
      origin: "observed",
      assurance: "proposed",
      confidence: 0.5,
      impacts: ["envelope"],
      evidence_ids: ["evidence:whole-sheet"],
      state: "active",
    }, {
      id: "assertion:operation:outer:kind",
      subject_id: "operation:outer",
      predicate: "operation.kind",
      value: { kind: "exact", value: "extrude" },
      origin: "derived",
      assurance: "corroborated",
      confidence: 0.8,
      impacts: ["base_topology"],
      evidence_ids: ["evidence:whole-sheet"],
      state: "active",
    }, {
      id: "assertion:operation:outer:sequence",
      subject_id: "operation:outer",
      predicate: "operation.sequence",
      value: { kind: "exact", value: 0 },
      origin: "derived",
      assurance: "constraint_validated",
      confidence: 1,
      impacts: ["base_topology"],
      evidence_ids: [],
      state: "active",
    }, {
      id: "assertion:feature:outer:diameter",
      subject_id: "feature:outer",
      predicate: "feature.param.diameter_mm",
      value: { kind: "exact", value: 15.7 },
      origin: "observed",
      assurance: "proposed",
      confidence: 0.7,
      impacts: ["base_topology"],
      evidence_ids: ["evidence:whole-sheet"],
      state: "active",
    }],
    evidence: [{ id: "evidence:whole-sheet", kind: "raster_region", source_region_id: "region:hole", payload: { bbox_normalized: [0.1, 0.2, 0.4, 0.5], fallback: false } }],
    hypothesis_sets: [],
    build_targets: [{ id: "preview", kind: "preview_brep", root_node_ids: ["product:legacy-spec"], requirement_ids: [], critical_impacts: ["connection_opening"], mass_tolerance_percent: 0 }],
    verification: { critical_unresolved_assertion_ids: ["assertion:hole-location"], issue_codes: ["critical_assertions_unresolved"] },
    reader_manifest: { max_wall_seconds: 900, max_model_calls: 32, call_timeout_seconds: 90, no_progress_pass_limit: 2, calls_used: 3, elapsed_seconds: 428.7, no_progress_passes: 2, ordinary_attempts: { "assertion:hole-location": 3 }, stop_reason: "fixed_point" },
  },
};

// Keep the original digitization fixture intentionally small. The editor
// fixture extends it with a BuildOperation/Feature/topology chain without
// changing the established Graph Inspector scenarios below.
const graph = {
  ...editorGraph,
  graph: {
    ...editorGraph.graph,
    nodes: editorGraph.graph.nodes.filter((item) =>
      ["product:legacy-spec", "region:hole"].includes(item.id),
    ),
    edges: [],
    assertions: editorGraph.graph.assertions.filter((item) =>
      ["assertion:hole-location", "assertion:shaft-length"].includes(item.id),
    ),
  },
};

const generation = {
  id: generationId,
  operation: "vectorize",
  status: "failed",
  progress: null,
  prompt: "Оцифровка вала",
  negative_prompt: null,
  params: {
    engineering_model_graph: { revision_id: graph.id },
    spec: { part: "Проблемный вал", dimensions: [{ value: "Ø15.7" }] },
  },
  source_image_paths: ["image-gen/dev-user/shaft.png"],
  mask_path: null,
  has_result: false,
  error: "Критические данные чертежа не подтверждены",
  parent_id: null,
  accepted: false,
  workflow_id: null,
  created_at: "2026-08-09T12:01:49Z",
  source_document_id: null,
  case_id: null,
};

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([{ name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" }]);
}

async function mockApi(page: Page, modelGraph: typeof editorGraph = graph) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/auth/me") return route.fulfill({ json: { sub: "e2e", email: "e2e@example.local", name: "E2E", roles: ["admin"], groups: [] } });
    if (url.pathname === `/api/image-gen/${generationId}`) return route.fulfill({ json: generation });
    if (url.pathname === `/api/image-gen/${generationId}/model-graph`) return route.fulfill({ json: modelGraph });
    if (url.pathname.endsWith("/model-graph/design-history")) return route.fulfill({ json: { graph_id: modelGraph.graph_id, graph_role: "design", current_revision: 0, revisions: [{ revision: 0, parent_revision: null, canonical_sha256: modelGraph.canonical_sha256, created_at: "2026-08-09T12:01:49Z", actor_id: "e2e", reason: "Initial design" }] } });
    if (url.pathname.endsWith("/certification")) return route.fulfill({ json: { status: "draft", revision: 0, drafter_approved_at: null, normcontroller_approved_at: null } });
    if (url.pathname.endsWith("/model-graph/patches")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/model-graph/trace-proposals")) return route.fulfill({ json: [] });
    if (url.pathname.includes("/model-graph/assertions/") && url.pathname.endsWith("/impact")) {
      return route.fulfill({ json: { assertion_id: "assertion:operation:outer:kind", target_id: "preview", subject_node_id: "operation:outer", critical_for_target: true, classification: "critical_for_target", declared_impacts: ["base_topology"], direct_dependency_node_ids: ["topology:face:outer"], affected_node_ids: ["operation:outer", "topology:face:outer", "artifact:step"], affected_build_operation_ids: ["operation:outer"], affected_artifact_ids: ["artifact:step"], affected_topology_element_ids: ["topology:face:outer"], evidence_ids: ["evidence:whole-sheet"], superseded_by_assertion_ids: [], dependency_paths: { "topology:face:outer": ["operation:outer", "feature:outer", "topology:face:outer"] } } });
    }
    if (url.pathname.endsWith("/source-overlay")) {
      return route.fulfill({
        contentType: "image/png",
        body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z2S8AAAAASUVORK5CYII=", "base64"),
      });
    }
    if (url.pathname.endsWith("/model-graph/corrections") && request.method() === "POST") {
      batchCorrectionCalls += 1;
      return route.fulfill({
        json: {
          ...graph,
          id: "88888888-8888-4888-8888-888888888888",
          revision: 2,
          parent_revision: 1,
          corrected_assertion_ids: ["assertion:hole-location", "assertion:shaft-length"],
          compatibility_spec_updated: true,
          rebuild_task_id: null,
        },
      });
    }
    if (url.pathname.endsWith("/corrections") && request.method() === "POST") {
      correctionCalls += 1;
      const replacement = {
        ...graph.graph.assertions[0],
        id: "assertion:human:hole-location",
        value: { kind: "exact", value: 15.7 },
        origin: "human",
        assurance: "human_approved",
        confidence: 1,
        supersedes_assertion_id: "assertion:hole-location",
      };
      return route.fulfill({
        json: {
          ...graph,
          id: "77777777-7777-4777-8777-777777777777",
          revision: 1,
          parent_revision: 0,
          graph: {
            ...graph.graph,
            assertions: [
              { ...graph.graph.assertions[0], state: "superseded" },
              replacement,
              ...graph.graph.assertions.slice(1),
            ],
          },
          compatibility_spec_updated: true,
          rebuild_task_id: "rebuild-task",
        },
      });
    }
    if (url.pathname.endsWith("/model-graph/verify") && request.method() === "POST") {
      verifyCalls += 1;
      return route.fulfill({ json: { workflow_status: "review_required", state: { comprehension: "review_needed", build: "not_ready", release: "blocked" }, issues: [] } });
    }
    if (url.pathname.endsWith("/model-graph/reader-runs") && request.method() === "POST") {
      readerCalls += 1;
      return route.fulfill({ json: { task_id: "reader-task", graph_id: graph.graph_id, base_revision: 0, target_id: "preview" } });
    }
    if (url.pathname === "/api/tasks/reader-task") return route.fulfill({ json: { task_id: "reader-task", status: "SUCCESS", result: {} } });
    if (url.pathname === "/api/tasks/rebuild-task") return route.fulfill({ json: { task_id: "rebuild-task", status: "SUCCESS", result: {} } });
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET") return route.fulfill({ json: [] });
    if (url.pathname === "/api/dashboard/feed") return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count" || url.pathname === "/api/notifications/unread-count") return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/ai/agent-config") return route.fulfill({ json: {} });
    return route.fulfill({ json: {} });
  });
}

test("blocked DXF digitization opens canonical EMG before legacy spec", async ({ page, context }) => {
  verifyCalls = 0;
  readerCalls = 0;
  correctionCalls = 0;
  batchCorrectionCalls = 0;
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/cad/${generationId}`);

  await expect(page.getByRole("button", { name: "Engineering Model Graph" })).toHaveClass(/bg-sky/);
  await expect(page.getByText("Чтение и граф сохранены; построение заблокировано до уточнения critical assertions.")).toBeVisible();
  await expect(page.getByText("hole.location_mm", { exact: true })).toBeVisible();
  await expect(page.getByText("Выпуск: blocked")).toBeVisible();
  await expect(page.getByRole("link", { name: "Скачать .emg.json" })).toHaveAttribute(
    "href",
    `/api/image-gen/${generationId}/model-graph/download`,
  );
  await expect(page.getByText(/SourceRegion: region:hole · точный ROI/)).toBeVisible();
  await expect(page.getByAltText("Исходный crop")).toBeVisible();

  const selector = page.getByRole("application", { name: "Выбор SourceRegion на исходном листе" });
  const selectorBox = await selector.boundingBox();
  if (!selectorBox) throw new Error("bbox selector is not visible");
  await page.mouse.move(selectorBox.x + selectorBox.width * 0.1, selectorBox.y + selectorBox.height * 0.2);
  await page.mouse.down();
  await page.mouse.move(selectorBox.x + selectorBox.width * 0.4, selectorBox.y + selectorBox.height * 0.5);
  await page.mouse.up();
  await expect(page.getByLabel("Source bbox normalized")).not.toHaveValue("");
  await page.getByRole("button", { name: "Добавить в атомарный пакет" }).click();
  await expect(page.getByText("Атомарная пакетная правка (1)")).toBeVisible();

  await page.getByLabel("AssertionValue JSON").fill('{"kind":"exact","value":15.7}');
  await page.getByLabel("Инженерное обоснование").fill("Подтверждено по выноске Ø15.7");
  await page.getByText("После GraphPatch пересобрать provisional DXF без повторного VLM-чтения").click();
  await page.getByRole("button", { name: "Создать human GraphPatch" }).click();
  await expect.poll(() => correctionCalls).toBe(1);
  await expect(page.getByText(/GraphPatch принят: revision 1 · compatibility-view обновлён · пересборка поставлена в очередь/)).toBeVisible();
  await expect(page.getByText("Принято инженером").first()).toBeVisible();

  await page.getByRole("button", { name: "Проверить 12 уровней" }).click();
  await expect.poll(() => verifyCalls).toBe(1);
  await page.getByRole("button", { name: "Адаптивно дочитать" }).click();
  await expect.poll(() => readerCalls).toBe(1);

  await page.getByRole("button", { name: "Чертёж и compatibility-view" }).click();
  await expect(page.getByText("Что удалось прочитать с листа")).toBeVisible();
  await expect(page.getByText("Размеры (1):")).toBeVisible();
});

test("CAD editor shows selected operation source bbox and topology impact", async ({ page, context }) => {
  await setAuthCookie(context);
  await mockApi(page, editorGraph);
  await page.goto(`/cad/${generationId}/editor`);

  const panel = page.getByTestId("operation-provenance-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("Операция → утверждение → bbox → зависимые узлы")).toBeVisible();
  await expect(panel.getByAltText("Источник feature.param.diameter_mm")).toBeVisible();
  await expect(panel.getByText(/лист 1 · bbox 0.100, 0.200, 0.400, 0.500 · точный ROI/)).toBeVisible();
  await expect(panel.getByText(/Критично для сборки preview/)).toBeVisible();
  await expect(panel.getByText(/Outer face \(topology:face:outer\)/)).toBeVisible();
  await expect(panel.getByText(/STEP model \(artifact:step\)/)).toBeVisible();
});

test("related assertions are submitted as one atomic GraphPatch", async ({ page, context }) => {
  batchCorrectionCalls = 0;
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/cad/${generationId}`);

  await page.getByLabel("Инженерное обоснование").fill("Связанная размерная цепь проверена");
  await page.getByRole("button", { name: "Добавить в атомарный пакет" }).click();
  await page.getByRole("button", { name: /length_mm/ }).click();
  await page.getByLabel("Инженерное обоснование").fill("Связанная размерная цепь проверена");
  await page.getByRole("button", { name: "Добавить в атомарный пакет" }).click();
  await expect(page.getByText("Атомарная пакетная правка (2)")).toBeVisible();
  await page.getByRole("button", { name: "Применить пакет одним GraphPatch" }).click();

  await expect.poll(() => batchCorrectionCalls).toBe(1);
  await expect(page.getByText(/Атомарный GraphPatch принят: 2 assertions · revision 2/)).toBeVisible();
});
