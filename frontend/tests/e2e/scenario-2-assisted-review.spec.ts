/**
 * Scenario 2: Assisted Review
 * Flow: user opens document in review mode, sees extracted fields,
 * corrects a field, approves the document.
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

const DOC_ID = "cccccccc-2222-4000-8000-000000000001";

const doc = {
  id: DOC_ID,
  file_name: "invoice-assisted-review.pdf",
  file_hash: "abc123",
  file_size: 12345,
  mime_type: "application/pdf",
  page_count: 2,
  doc_type: "invoice",
  doc_type_confidence: 0.97,
  status: "needs_review",
  source_channel: "upload",
  created_at: "2026-05-19T09:00:00Z",
  updated_at: "2026-05-19T09:05:00Z",
  extractions: [
    {
      id: "ext-0001",
      model_name: "gemma4:e4b",
      overall_confidence: 0.91,
      fields: [
        {
          field_name: "supplier_name",
          field_value: "ООО АCME",
          confidence: 0.98,
          confidence_reason: "high_match",
          human_corrected: false,
        },
        {
          field_name: "total_amount",
          field_value: "15000",
          confidence: 0.55,
          confidence_reason: "low_ocr_quality",
          human_corrected: false,
        },
        {
          field_name: "invoice_date",
          field_value: "2026-05-18",
          confidence: 0.99,
          confidence_reason: "exact_match",
          human_corrected: false,
        },
      ],
      created_at: "2026-05-19T09:05:00Z",
    },
  ],
  links: [],
};

const extraction = {
  document_id: DOC_ID,
  status: "done",
  fields: doc.extractions[0].fields.map((f) => ({
    field_name: f.field_name,
    value: f.field_value,
    confidence: f.confidence,
    source: "llm",
    human_corrected: f.human_corrected,
  })),
  raw_text: "invoice content here",
  created_at: "2026-05-19T09:05:00Z",
  model_used: "gemma4:e4b",
};

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (
      url.pathname === `/api/documents/${DOC_ID}` &&
      request.method() === "GET"
    )
      return route.fulfill({ json: doc });

    if (url.pathname === `/api/documents/${DOC_ID}/extraction`)
      return route.fulfill({ json: extraction });

    if (url.pathname === `/api/documents/${DOC_ID}/review-queue`)
      return route.fulfill({ json: { ids: [DOC_ID], total: 1 } });

    if (
      url.pathname === `/api/documents/${DOC_ID}/approve` &&
      request.method() === "POST"
    )
      return route.fulfill({ json: { ...doc, status: "approved" } });

    if (
      url.pathname === `/api/documents/${DOC_ID}` &&
      request.method() === "PATCH"
    )
      return route.fulfill({ json: { ...doc, status: "approved" } });

    if (url.pathname === "/api/documents" && request.method() === "GET")
      return route.fulfill({ json: { items: [doc], total: 1 } });

    if (url.pathname === "/api/documents/workspace")
      return route.fulfill({ json: { items: [], total: 0 } });

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

    // Shared stubs
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
      return route.fulfill({ json: { total: 0, items: [] } });
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
          id: "chat-e2e",
          title: "Новый чат",
          created_at: "2026-05-19T12:00:00Z",
          updated_at: "2026-05-19T12:00:00Z",
        },
      });
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("Scenario 2: review page shows document filename and extracted fields", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}/review`);

  // Document name in header
  await expect(page.getByText("invoice-assisted-review.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // Extracted field should appear
  await expect(page.getByText("supplier_name").first()).toBeAttached({
    timeout: 5_000,
  });
});

test("Scenario 2: field confidence badges show percentages", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}/review`);

  await expect(page.getByText("invoice-assisted-review.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // total_amount has confidence 0.55 → "55%" badge in ExtractionPanel
  await expect(page.getByText("55%").first()).toBeAttached({ timeout: 5_000 });
});

test("Scenario 2: overall confidence badge shows percentage", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}/review`);

  await expect(page.getByText("invoice-assisted-review.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // overall_confidence = 0.91 → "91%" badge
  await expect(page.getByText("91%").first()).toBeAttached({ timeout: 5_000 });
});
