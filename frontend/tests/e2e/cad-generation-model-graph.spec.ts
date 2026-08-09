import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

const generationId = "55555555-5555-4555-8555-555555555555";
let verifyCalls = 0;
let readerCalls = 0;
let correctionCalls = 0;

const graph = {
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
    ],
    edges: [],
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
    }],
    evidence: [{ id: "evidence:whole-sheet", kind: "raster_region", source_region_id: "region:hole", payload: { bbox_normalized: [0.1, 0.2, 0.4, 0.5], fallback: false } }],
    hypothesis_sets: [],
    build_targets: [{ id: "preview", kind: "preview_brep", root_node_ids: ["product:legacy-spec"], requirement_ids: [], critical_impacts: ["connection_opening"], mass_tolerance_percent: 0 }],
    verification: { critical_unresolved_assertion_ids: ["assertion:hole-location"], issue_codes: ["critical_assertions_unresolved"] },
    reader_manifest: { max_wall_seconds: 900, max_model_calls: 32, call_timeout_seconds: 90, no_progress_pass_limit: 2, calls_used: 3, elapsed_seconds: 428.7, no_progress_passes: 2, ordinary_attempts: { "assertion:hole-location": 3 }, stop_reason: "fixed_point" },
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

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/auth/me") return route.fulfill({ json: { sub: "e2e", email: "e2e@example.local", name: "E2E", roles: ["admin"], groups: [] } });
    if (url.pathname === `/api/image-gen/${generationId}`) return route.fulfill({ json: generation });
    if (url.pathname === `/api/image-gen/${generationId}/model-graph`) return route.fulfill({ json: graph });
    if (url.pathname.endsWith("/model-graph/patches")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/model-graph/trace-proposals")) return route.fulfill({ json: [] });
    if (url.pathname.includes("/model-graph/assertions/") && url.pathname.endsWith("/impact")) {
      return route.fulfill({ json: { assertion_id: "assertion:hole-location", target_id: "preview", subject_node_id: "product:legacy-spec", critical_for_target: true, classification: "critical_for_target", declared_impacts: ["connection_opening"], direct_dependency_node_ids: [], affected_node_ids: ["product:legacy-spec"], affected_build_operation_ids: [], affected_artifact_ids: [], affected_topology_element_ids: [], evidence_ids: ["evidence:whole-sheet"], superseded_by_assertion_ids: [], dependency_paths: { "product:legacy-spec": ["product:legacy-spec"] } } });
    }
    if (url.pathname.endsWith("/source-overlay")) {
      return route.fulfill({
        contentType: "image/png",
        body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z2S8AAAAASUVORK5CYII=", "base64"),
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
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/cad/${generationId}`);

  await expect(page.getByRole("button", { name: "Engineering Model Graph" })).toHaveClass(/bg-sky/);
  await expect(page.getByText("Чтение и граф сохранены; построение заблокировано до уточнения critical assertions.")).toBeVisible();
  await expect(page.getByText("hole.location_mm", { exact: true })).toBeVisible();
  await expect(page.getByText("Выпуск: blocked")).toBeVisible();
  await expect(page.getByText(/SourceRegion: region:hole · точный ROI/)).toBeVisible();
  await expect(page.getByAltText("Исходный crop")).toBeVisible();

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
