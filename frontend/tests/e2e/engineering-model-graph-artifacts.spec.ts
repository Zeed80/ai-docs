import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

const projectId = "11111111-1111-4111-8111-111111111111";
const engineeringRevisionId = "22222222-2222-4222-8222-222222222222";
const graphRevisionId = "33333333-3333-4333-8333-333333333333";
const artifactId = "artifact:system-diagram:0123456789abcdef";
let buildCalls = 0;

const graph = {
  id: graphRevisionId,
  engineering_project_id: projectId,
  engineering_revision_id: engineeringRevisionId,
  graph_id: `system:${engineeringRevisionId}`,
  revision: 2,
  parent_revision: 1,
  canonical_sha256: "a".repeat(64),
  profile: "hydraulic",
  comprehension_status: "converged",
  build_status: "verified",
  release_status: "approved",
  graph: {
    nodes: [{ id: "system:root", type: "System", name: "Power unit" }, { id: artifactId, type: "Artifact", name: "System diagram SVG" }],
    edges: [],
    assertions: [{
      id: "assertion:diagram-media",
      subject_id: artifactId,
      predicate: "artifact.media_type",
      value: { kind: "exact", value: "image/svg+xml" },
      origin: "derived",
      assurance: "constraint_validated",
      confidence: 1,
      impacts: [],
      evidence_ids: ["evidence:diagram"],
      state: "active",
    }],
    evidence: [{
      id: "evidence:diagram",
      kind: "projection_comparison",
      source_region_id: null,
      payload: {
        artifact_path: "engineering/systems/example.svg",
        report_path: "engineering/systems/example.json",
        artifact_sha256: "b".repeat(64),
        media_type: "image/svg+xml",
        views: ["system-diagram"],
        required_views_complete: true,
        valid: true,
      },
    }],
    hypothesis_sets: [],
    build_targets: [{ id: "production", kind: "production_pdf", root_node_ids: ["system:root"], requirement_ids: [], critical_impacts: ["connectivity"], mass_tolerance_percent: 0 }],
    verification: { critical_unresolved_assertion_ids: [], issue_codes: [] },
    reader_manifest: { max_wall_seconds: 900, max_model_calls: 32, call_timeout_seconds: 90, no_progress_pass_limit: 2, calls_used: 0, elapsed_seconds: 0, no_progress_passes: 0, ordinary_attempts: {}, stop_reason: "converged" },
  },
};

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([{ name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" }]);
}

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/auth/me") {
      return route.fulfill({ json: { sub: "e2e", email: "e2e@example.local", name: "E2E", roles: ["admin"], groups: [] } });
    }
    if (url.pathname === `/api/engineering/projects/${projectId}`) {
      return route.fulfill({ json: { id: projectId, name: "Hydraulic test", code: "HYD-1", status: "validated", description: null, created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z", revisions: [{ id: engineeringRevisionId, engineering_project_id: projectId, revision: 1, base_revision: null, status: "validated", origin: "human", change_summary: null, validation: {}, created_at: "2026-08-09T00:00:00Z" }] } });
    }
    if (url.pathname === "/api/engineering/materials") return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/analysis-cases")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/projections")) return route.fulfill({ json: [] });
    if (url.pathname === `/api/engineering-model-graphs/projects/${projectId}/graphs`) return route.fulfill({ json: [graph] });
    if (url.pathname.endsWith("/patches")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/trace-proposals")) return route.fulfill({ json: [] });
    if (url.pathname.includes("/assertions/") && url.pathname.endsWith("/impact")) {
      return route.fulfill({ json: { assertion_id: "assertion:diagram-media", target_id: "production", subject_node_id: artifactId, critical_for_target: false, classification: "non_critical_for_target", declared_impacts: [], direct_dependency_node_ids: [], affected_node_ids: [], affected_build_operation_ids: [], affected_artifact_ids: [], affected_topology_element_ids: [], evidence_ids: ["evidence:diagram"], superseded_by_assertion_ids: [], dependency_paths: {} } });
    }
    if (url.pathname === `/api/engineering/revisions/${engineeringRevisionId}/system-model-graph/diagram` && request.method() === "POST") {
      buildCalls += 1;
      return route.fulfill({ json: { graph_id: graph.graph_id, revision: 2, canonical_sha256: graph.canonical_sha256, artifact_path: "engineering/systems/example.svg", artifact_sha256: "b".repeat(64), report_path: "engineering/systems/example.json", views: ["system-diagram"], production_export_allowed: true, critical_assumption_ids: [], idempotent_replay: true } });
    }
    if (url.pathname.includes(`/api/engineering-model-graphs/revisions/${graphRevisionId}/artifacts/`)) {
      if (url.searchParams.get("kind") === "report") return route.fulfill({ json: { valid: true } });
      return route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>' });
    }
    if (url.pathname === "/api/dashboard/feed") return route.fulfill({ json: { total: 0, items: [] } });
    if (url.pathname === "/api/quarantine/count" || url.pathname === "/api/notifications/unread-count") return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET") return route.fulfill({ json: [] });
    if (url.pathname === "/api/ai/agent-config") return route.fulfill({ json: {} });
    return route.fulfill({ json: {} });
  });
}

test("Engineering Graph Inspector previews, downloads and rebuilds a domain artifact", async ({ page, context }) => {
  buildCalls = 0;
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/engineering/${projectId}`);

  await expect(page.getByText("System diagram SVG", { exact: true })).toBeVisible();
  await expect(page.getByText("SHA verified")).toBeVisible();
  await expect(page.getByText("Виды: system-diagram")).toBeVisible();
  await expect(page.getByRole("link", { name: "Открыть artifact" })).toHaveAttribute("href", new RegExp(encodeURIComponent(artifactId)));

  await page.getByRole("button", { name: "Построить схему системы" }).click();
  await expect.poll(() => buildCalls).toBe(1);
  await expect(page.getByText(/Уже существовал: bbbbbbbbbbbb/)).toBeVisible();
});
