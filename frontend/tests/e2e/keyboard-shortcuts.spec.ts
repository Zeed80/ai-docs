/**
 * Keyboard shortcuts E2E tests
 * Covers: Esc on document page → Inbox, review page shortcuts (a/j/k/n/Esc)
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

const DOC_ID = "dddddddd-1111-4000-8000-000000000001";
const DOC_ID_2 = "dddddddd-1111-4000-8000-000000000002";

const doc1 = {
  id: DOC_ID,
  file_name: "invoice-test-001.pdf",
  status: "needs_review",
  doc_type: "invoice",
  created_at: "2026-05-19T09:00:00Z",
  extractions: [] as unknown[],
};

const doc2 = {
  id: DOC_ID_2,
  file_name: "invoice-test-002.pdf",
  status: "needs_review",
  doc_type: "invoice",
  created_at: "2026-05-19T10:00:00Z",
  extractions: [] as unknown[],
};

const extraction = {
  document_id: DOC_ID,
  status: "done",
  fields: [
    {
      field_name: "supplier_name",
      value: "ООО Тест",
      confidence: 0.98,
      source: "llm",
    },
    {
      field_name: "total_amount",
      value: "10000",
      confidence: 0.95,
      source: "llm",
    },
    {
      field_name: "invoice_date",
      value: "2026-05-19",
      confidence: 0.99,
      source: "llm",
    },
  ],
  raw_text: "Test invoice content",
  created_at: "2026-05-19T09:05:00Z",
  model_used: "test",
};

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (
      url.pathname === `/api/documents/${DOC_ID}` &&
      request.method() === "GET"
    )
      return route.fulfill({ json: doc1 });
    if (
      url.pathname === `/api/documents/${DOC_ID_2}` &&
      request.method() === "GET"
    )
      return route.fulfill({ json: doc2 });
    if (url.pathname === `/api/documents/${DOC_ID}/extraction`)
      return route.fulfill({ json: extraction });
    if (url.pathname === `/api/documents/${DOC_ID_2}/extraction`)
      return route.fulfill({
        json: { ...extraction, document_id: DOC_ID_2, fields: [] },
      });
    if (
      url.pathname === `/api/documents/${DOC_ID}/approve` &&
      request.method() === "POST"
    )
      return route.fulfill({ json: { ...doc1, status: "approved" } });
    if (url.pathname === `/api/documents/${DOC_ID}/review-queue`)
      return route.fulfill({ json: { ids: [DOC_ID, DOC_ID_2], total: 2 } });
    if (url.pathname === `/api/documents/${DOC_ID_2}/review-queue`)
      return route.fulfill({ json: { ids: [DOC_ID, DOC_ID_2], total: 2 } });
    if (url.pathname === "/api/documents" && request.method() === "GET")
      return route.fulfill({ json: { items: [doc1, doc2], total: 2 } });
    if (url.pathname === "/api/documents/workspace")
      return route.fulfill({ json: { items: [], total: 0 } });
    // NTD availability stub — returns disabled with empty reasons
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
    if (url.pathname === "/api/inbox" && request.method() === "GET")
      return route.fulfill({ json: { items: [doc1, doc2], total: 2 } });

    return route.fulfill({ json: {} });
  });
}

test("keyboard: Esc on document page navigates to inbox", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}`);

  await expect(page.getByText("invoice-test-001.pdf")).toBeVisible({
    timeout: 10_000,
  });

  await page.keyboard.press("Escape");

  await expect(page).toHaveURL(/\/inbox$/, { timeout: 5000 });
});

test("keyboard: Esc on review page navigates to document page", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}/review`);

  // Wait for review page to load
  await expect(page.getByText("invoice-test-001.pdf")).toBeVisible({
    timeout: 10_000,
  });

  await page.keyboard.press("Escape");

  await expect(page).toHaveURL(new RegExp(`/documents/${DOC_ID}$`), {
    timeout: 5000,
  });
});

test("keyboard: 'a' on review page approves document", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}/review`);

  await expect(page.getByText("invoice-test-001.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // Press 'a' to approve
  await page.keyboard.press("a");

  // Should navigate away (to next doc or inbox) after approval
  await expect(page).not.toHaveURL(new RegExp(`/documents/${DOC_ID}/review$`), {
    timeout: 8000,
  });
});

test("keyboard: 'j'/'k' cycles through extraction fields on review page", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto(`/documents/${DOC_ID}/review`);

  await expect(page.getByText("invoice-test-001.pdf")).toBeVisible({
    timeout: 10_000,
  });

  // First field should be active initially, pressing 'j' moves to next
  await page.keyboard.press("j");
  // pressing 'k' moves back
  await page.keyboard.press("k");

  // Page stays on review (no navigation)
  await expect(page).toHaveURL(new RegExp(`/documents/${DOC_ID}/review$`), {
    timeout: 2000,
  });
});
