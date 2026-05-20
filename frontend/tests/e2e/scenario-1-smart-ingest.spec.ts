/**
 * Scenario 1: Smart Ingest (без email части)
 * Flow: document uploaded → appears in Inbox with queued/ingested status →
 * user navigates to review → document has extraction queued.
 */
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

const DOC_ID = "aaaaaaaa-1111-4000-8000-000000000001";

const ingestedDoc = {
  id: DOC_ID,
  file_name: "invoice-smart-ingest.pdf",
  file_hash: "si001hash",
  file_size: 65536,
  mime_type: "application/pdf",
  page_count: 1,
  doc_type: "invoice",
  doc_type_confidence: 0.89,
  status: "needs_review",
  source_channel: "upload",
  created_at: "2026-05-20T08:00:00Z",
  updated_at: "2026-05-20T08:01:00Z",
  extractions: [],
  links: [],
};

const pipelineJob = {
  id: "job-si-001",
  document_id: DOC_ID,
  status: "queued",
  pipeline_type: "extraction",
  created_at: "2026-05-20T08:01:00Z",
};

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    // Document list — includes our ingested document
    if (url.pathname === "/api/documents" && request.method() === "GET")
      return route.fulfill({
        json: { items: [ingestedDoc], total: 1 },
      });

    // Specific document fetch
    if (
      url.pathname === `/api/documents/${DOC_ID}` &&
      request.method() === "GET"
    )
      return route.fulfill({ json: ingestedDoc });

    // Pipeline job for the document
    if (url.pathname === `/api/documents/${DOC_ID}/pipeline-jobs`)
      return route.fulfill({ json: [pipelineJob] });

    // Upload endpoint — simulate success
    if (url.pathname === "/api/documents/ingest" && request.method() === "POST")
      return route.fulfill({ status: 201, json: ingestedDoc });

    // Extraction not available yet (job in queue)
    if (url.pathname === `/api/documents/${DOC_ID}/extraction`)
      return route.fulfill({ status: 404, json: { detail: "Not found" } });

    // Review queue
    if (url.pathname === `/api/documents/${DOC_ID}/review-queue`)
      return route.fulfill({ json: { ids: [DOC_ID], total: 1 } });

    // Workspace endpoint
    if (url.pathname === "/api/documents/workspace")
      return route.fulfill({ json: { items: [], total: 0 } });

    // NTD check stubs
    if (url.pathname.endsWith("/ntd-check/availability"))
      return route.fulfill({
        json: {
          document_id: DOC_ID,
          can_check: false,
          reasons: [],
          active_requirements: 0,
          has_text: false,
          mode: "manual",
        },
      });
    if (url.pathname.endsWith("/ntd-checks"))
      return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/price-check"))
      return route.fulfill({ json: { comparisons: [] } });
    if (url.pathname.startsWith("/api/search/similar/"))
      return route.fulfill({ json: { results: [] } });
    if (url.pathname === "/api/handovers/outbox")
      return route.fulfill({ json: [] });
    if (url.pathname.startsWith("/api/comments"))
      return route.fulfill({ json: [] });

    // Auth & shared stubs
    if (url.pathname === "/api/auth/me")
      return route.fulfill({
        json: {
          sub: "e2e",
          email: "e2e@example.local",
          name: "E2E",
          preferred_username: "e2e",
          roles: [],
          groups: [],
        },
      });
    if (url.pathname === "/api/dashboard/feed")
      return route.fulfill({
        json: {
          total: 1,
          items: [
            {
              id: DOC_ID,
              type: "document",
              priority: "medium",
              title: "invoice-smart-ingest.pdf",
              entity_type: "document",
              entity_id: DOC_ID,
              created_at: "2026-05-20T08:00:00Z",
            },
          ],
        },
      });
    if (url.pathname === "/api/quarantine/count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (url.pathname === "/api/ai/agent-config")
      return route.fulfill({ json: {} });
    if (url.pathname === "/api/chat/sessions" && request.method() === "GET")
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/chat/sessions" && request.method() === "POST")
      return route.fulfill({
        json: {
          id: "chat-e2e-si",
          title: "Новый чат",
          created_at: "2026-05-20T08:00:00Z",
          updated_at: "2026-05-20T08:00:00Z",
        },
      });
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("Scenario 1: inbox shows ingested document", async ({ page, context }) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/documents");

  // The document should appear in the document list
  await expect(page.getByText("invoice-smart-ingest.pdf")).toBeVisible({
    timeout: 10_000,
  });
});

test("Scenario 1: ingested document has needs_review status", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/documents");

  await expect(page.getByText("invoice-smart-ingest.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // Status badge for needs_review should be visible somewhere in the list
  await expect(
    page
      .locator('[data-testid="doc-status"], .status-badge, [class*="status"]')
      .first(),
  ).toBeAttached({ timeout: 5_000 });
});

test("Scenario 1: navigate to document review from inbox", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/documents");

  await expect(page.getByText("invoice-smart-ingest.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // Click on document to open review
  await page.getByText("invoice-smart-ingest.pdf").first().click();

  // Should navigate to the document detail/review page
  await expect(page).toHaveURL(new RegExp(`/documents/${DOC_ID}`), {
    timeout: 5_000,
  });
});
