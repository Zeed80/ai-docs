import { expect, test, type Page, type Route } from "@playwright/test";

const agentConfig = {
  enabled: true,
  agent_name: "Света",
  model: "qwen3.5:9b",
  department_enabled: true,
  orchestrator_model: null,
  orchestrator_provider: null,
  orchestrator_disable_thinking: false,
  worker_model: null,
  worker_provider: null,
  worker_disable_thinking: false,
  auditor_model: null,
  auditor_provider: null,
  auditor_disable_thinking: false,
  builder_model: null,
  builder_provider: null,
  builder_disable_thinking: false,
  fast_model: null,
  fast_provider: null,
  fast_disable_thinking: false,
  provider: "ollama",
  fallback_providers: [],
  prompt_cache_enabled: false,
  disable_thinking: false,
  ollama_url: "http://localhost:11434",
  vllm_url: "http://localhost:8001/v1",
  lmstudio_url: "http://localhost:1234/v1",
  openai_compatible_url: "http://localhost:8001/v1",
  backend_url: "http://localhost:8000",
  temperature: 0.1,
  max_steps: 10,
  llm_timeout_seconds: 180,
  backend_timeout_seconds: 30,
  approval_timeout_seconds: 120,
  max_worker_steps: 12,
  max_audit_retries: 1,
  memory_enabled: true,
  audit_enabled: true,
  allow_capability_builder: true,
  capability_builder_requires_approval: true,
  autonomy_mode: "max_autonomy",
  permission_mode: "workspace_write",
  safe_auto_apply_enabled: true,
  max_history_messages: 40,
  exposed_skills: ["config.propose", "capability.propose", "capability.sandbox_apply"],
  approval_gates: [],
  system_prompt: null,
  context_compression_enabled: true,
  context_compression_threshold: 0.85,
  compression_model: null,
  mcp_servers: [],
};

const controlPlane = {
  ok: true,
  autonomy_mode: "max_autonomy",
  permission_mode: "workspace_write",
  safe_auto_apply_enabled: true,
  protected_settings: ["safe_auto_apply_enabled", "permission_mode", "system_prompt"],
  skills_total: 3,
  approval_gates_total: 0,
  plugins_total: 0,
  plugins_enabled: 0,
  tasks_open: 0,
  crons_enabled: 0,
  memory_facts_total: 0,
  mcp_servers_total: 0,
  capability_proposals_open: 1,
};

const configProposal = {
  id: "11111111-1111-4111-8111-111111111111",
  setting_path: "safe_auto_apply_enabled",
  proposed_value: { value: false },
  current_value: { value: true },
  reason: "E2E protected setting proposal",
  risk_level: "high",
  protected: true,
  status: "pending",
  requested_by: "e2e",
  decided_by: null,
  decided_at: null,
  decision_comment: null,
  created_at: "2026-05-06T12:00:00Z",
};

const capabilityProposal = {
  id: "22222222-2222-4222-8222-222222222222",
  title: "E2E capability proposal",
  missing_capability: "Need a generated workspace tool",
  reason: "E2E capability lifecycle",
  suggested_artifact: "tool",
  status: "draft",
  risk_level: "medium",
  sandbox_status: "not_started",
  test_status: "not_run",
  audit_status: "pending",
  draft: {
    tool_name: "workspace.e2e_tool",
    endpoint_path: "/api/workspace/agent/e2e-tool",
    implementation_plan: ["Generate sandbox artifact"],
  },
  rollback_plan: ["Discard sandbox"],
  requested_by: "e2e",
  created_at: "2026-05-06T12:00:00Z",
};

async function mockSettingsApi(page: Page, calls: string[]) {
  let configProposalOpen = true;
  let capability = { ...capabilityProposal };

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    calls.push(key);

    if (url.pathname === "/api/ai/agent-config") {
      return route.fulfill({ json: agentConfig });
    }
    if (url.pathname === "/api/ai/agent-skills") {
      return route.fulfill({
        json: {
          skills: [
            {
              name: "config.propose",
              description: "Propose config",
              method: "POST",
              path: "/api/agent/config/propose",
              enabled: true,
              approval_required: false,
            },
          ],
        },
      });
    }
    if (url.pathname === "/api/agent/control-plane/status") {
      return route.fulfill({ json: controlPlane });
    }
    if (url.pathname === "/api/agent/config/proposals" && request.method() === "GET") {
      return route.fulfill({ json: configProposalOpen ? [configProposal] : [] });
    }
    if (
      url.pathname === `/api/agent/config/proposals/${configProposal.id}/decide` &&
      request.method() === "POST"
    ) {
      configProposalOpen = false;
      return route.fulfill({ json: { ...configProposal, status: "approved" } });
    }
    if (url.pathname === "/api/agent/capabilities" && request.method() === "GET") {
      return route.fulfill({ json: [capability] });
    }
    if (
      url.pathname === `/api/agent/capabilities/${capabilityProposal.id}/sandbox-apply` &&
      request.method() === "POST"
    ) {
      capability = {
        ...capability,
        status: "sandbox_ready",
        sandbox_status: "ready",
        test_status: "passed",
      };
      return route.fulfill({ json: capability });
    }
    if (
      url.pathname === `/api/agent/capabilities/${capabilityProposal.id}/decide` &&
      request.method() === "POST"
    ) {
      capability = { ...capability, status: "approved" };
      return route.fulfill({ json: capability });
    }

    if (url.pathname === "/api/ai/config") return route.fulfill({ json: {} });
    if (url.pathname === "/api/ai/config/status") {
      return route.fulfill({ json: { ok: true, ollama_available: false, installed_models: [], warnings: [] } });
    }
    if (url.pathname === "/api/ai/models") return route.fulfill({ json: { models: [] } });
    if (url.pathname === "/api/ai/models/capabilities") {
      return route.fulfill({ json: { models: [] } });
    }
    if (url.pathname === "/api/ai/embedding-profile") {
      return route.fulfill({ json: { model_key: "none", provider_model: "none", collection_name: "none", dimension: 0, distance_metric: "cosine", normalize: true } });
    }
    if (url.pathname === "/api/memory/embeddings/stats") {
      return route.fulfill({ json: { active_model: "none", active_collection: "none", dimension: 0, counts_by_status: {}, total: 0 } });
    }
    if (url.pathname === "/api/settings/ntd-control") {
      return route.fulfill({ json: { mode: "manual", updated_by: null, updated_at: null } });
    }
    if (url.pathname === "/api/telegram/status") {
      return route.fulfill({
        json: {
          configured: false,
          bot_running: false,
          notifications_enabled: false,
          has_default_chat: false,
          allowed_users_count: 0,
          token_masked: "",
          chat_id_masked: "",
          allowed_users_masked: "",
          last_error: "",
        },
      });
    }
    if (url.pathname === "/api/auth/me") {
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E User",
          preferred_username: "e2e",
          roles: [],
          groups: [],
        },
      });
    }
    if (url.pathname === "/api/dashboard/feed") {
      return route.fulfill({ json: { total: 0, items: [] } });
    }
    if (url.pathname === "/api/quarantine/count") {
      return route.fulfill({ json: { count: 0 } });
    }
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET") {
      return route.fulfill({ json: [] });
    }
    if (url.pathname === "/api/chat/sessions" && request.method() === "POST") {
      return route.fulfill({
        json: {
          id: "chat-e2e",
          title: "Новый чат",
          created_at: "2026-05-06T12:00:00Z",
          updated_at: "2026-05-06T12:00:00Z",
        },
      });
    }
    if (url.pathname === "/api/chat/sessions/chat-e2e/messages") {
      return route.fulfill({ json: [] });
    }

    return route.fulfill({ json: {} });
  });
}

test("settings control plane approves protected config proposal", async ({ page }) => {
  const calls: string[] = [];
  await mockSettingsApi(page, calls);

  await page.goto("/settings");

  const panel = page.getByText("Ожидают подтверждения защищенные настройки").locator("..");
  await expect(panel.getByText("safe_auto_apply_enabled")).toBeVisible();
  await panel.getByRole("button", { name: "Разрешить" }).click();

  await expect(panel.getByText("safe_auto_apply_enabled")).toBeHidden();
  expect(calls).toContain(`POST /api/agent/config/proposals/${configProposal.id}/decide`);
});

test("settings control plane runs sandbox and approves capability proposal", async ({ page }) => {
  const calls: string[] = [];
  await mockSettingsApi(page, calls);

  await page.goto("/settings");

  const panel = page.getByText("Capability proposals").locator("..");
  await expect(panel.getByText("E2E capability proposal")).toBeVisible();
  await panel.getByRole("button", { name: "Sandbox" }).click();
  await expect(panel.getByText("Sandbox: ready")).toBeVisible();

  await panel.getByRole("button", { name: "Разрешить" }).click();
  await expect(panel.getByText("approved")).toBeVisible();
  expect(calls).toContain(`POST /api/agent/capabilities/${capabilityProposal.id}/sandbox-apply`);
  expect(calls).toContain(`POST /api/agent/capabilities/${capabilityProposal.id}/decide`);
});
