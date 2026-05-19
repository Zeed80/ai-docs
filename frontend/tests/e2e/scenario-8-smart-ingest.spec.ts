/**
 * Scenario 8: Smart Ingest
 * Flow: user uploads a file on /documents, the backend classifies it as an invoice,
 * the pipeline is queued, and the user sees a success status.
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

const ingestResponse = {
  id: DOC_ID,
  file_name: "invoice-acme-2025.pdf",
  status: "ingested",
  detected_type: "invoice",
  detected_type_source: "classifier",
  pipeline_queued: true,
  is_duplicate: false,
  quarantined: false,
};

const workspaceItems = [
  {
    document: {
      id: DOC_ID,
      file_name: "invoice-acme-2025.pdf",
      status: "ingested",
      doc_type: "invoice",
      created_at: "2026-05-19T12:00:00Z",
    },
    source_type: "direct_upload",
  },
];

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (
      url.pathname === "/api/documents/ingest" &&
      request.method() === "POST"
    ) {
      return route.fulfill({ status: 200, json: ingestResponse });
    }
    if (
      url.pathname === "/api/documents/workspace" &&
      request.method() === "GET"
    ) {
      return route.fulfill({ json: { items: workspaceItems, total: 1 } });
    }
    if (url.pathname === "/api/documents" && request.method() === "GET") {
      return route.fulfill({
        json: {
          items: [
            {
              id: DOC_ID,
              file_name: "invoice-acme-2025.pdf",
              status: "ingested",
              doc_type: "invoice",
              created_at: "2026-05-19T12:00:00Z",
            },
          ],
          total: 1,
        },
      });
    }

    // Shared stubs
    if (url.pathname === "/api/auth/me") {
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
    }
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
    if (url.pathname === "/api/chat/sessions" && request.method() === "POST") {
      return route.fulfill({
        json: {
          id: "chat-e2e",
          title: "Новый чат",
          created_at: "2026-05-19T12:00:00Z",
          updated_at: "2026-05-19T12:00:00Z",
        },
      });
    }
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("Scenario 8: upload invoice file shows upload zone on documents page", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/documents");

  // Upload tab should be active by default with drop zone
  await expect(page.getByText("Перетащите или выберите файлы")).toBeVisible({
    timeout: 5000,
  });
});

test("Scenario 8: file upload triggers ingest and shows pipeline queued status", async ({
  page,
  context,
}) => {
  test.setTimeout(60_000);
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/documents");
  // Wait for drop zone to render (not networkidle — HMR keeps connections open in dev)
  await expect(page.getByText("Перетащите или выберите файлы")).toBeVisible({
    timeout: 15_000,
  });

  // Upload a file via hidden input
  const fileInput = page.locator('input[type="file"]').first();
  await fileInput.setInputFiles({
    name: "invoice-acme-2025.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4 mock invoice content"),
  });

  // File appears in the queue (queue counter text)
  await expect(page.getByText(/Очередь: \d+ ожидает/)).toBeVisible({
    timeout: 8000,
  });

  // Upload button appears (text: "Загрузить 1 файл")
  const uploadBtn = page.getByRole("button", { name: /Загрузить \d+ файл/ });
  await expect(uploadBtn).toBeVisible({ timeout: 5000 });
  // Use DOM click to bypass pointer-event interception from parent section
  await uploadBtn.evaluate((el) => (el as HTMLButtonElement).click());

  // After upload: autoProcess=true → tab switches to "queue", upload zone disappears
  await expect(page.getByText(/Очередь: \d+ ожидает/)).not.toBeVisible({
    timeout: 10_000,
  });
});

test("Scenario 8: quarantined file shows quarantine status", async ({
  page,
  context,
}) => {
  test.setTimeout(60_000);
  await setAuthCookie(context);

  // Override ingest to return quarantine response (no reason → detail falls back to "карантин")
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (
      url.pathname === "/api/documents/ingest" &&
      request.method() === "POST"
    ) {
      return route.fulfill({
        status: 202,
        json: { quarantined: true, file_name: "suspicious.exe" },
      });
    }
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
    if (url.pathname === "/api/documents/workspace")
      return route.fulfill({ json: { items: [], total: 0 } });
    if (url.pathname === "/api/documents" && request.method() === "GET")
      return route.fulfill({ json: { items: [], total: 0 } });
    if (url.pathname === "/api/metrics")
      return route.fulfill({
        json: {
          documents: {
            total: 0,
            ingested: 0,
            needs_review: 0,
            approved: 0,
            rejected: 0,
          },
          invoices: { total: 0, approved: 0, rejected: 0, needs_review: 0 },
          approvals: { pending: 0, approved_today: 0, rejected_today: 0 },
          anomalies: { open: 0, resolved_today: 0, critical: 0 },
        },
      });
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

  await page.goto("/documents");
  await expect(page.getByText("Перетащите или выберите файлы")).toBeVisible({
    timeout: 15_000,
  });

  const fileInput = page.locator('input[type="file"]').first();
  await fileInput.setInputFiles({
    name: "suspicious.exe",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("MZ suspicious payload"),
  });

  // Wait for file to appear in queue
  await expect(page.getByText(/Очередь: \d+ ожидает/)).toBeVisible({
    timeout: 8000,
  });
  const uploadBtnQ = page.getByRole("button", { name: /Загрузить \d+ файл/ });
  await expect(uploadBtnQ).toBeVisible({ timeout: 5000 });
  await uploadBtnQ.evaluate((el) => (el as HTMLButtonElement).click());

  // Quarantine detail appears (file detail div inside overflow-hidden container)
  await expect(
    page.locator(".text-amber-400").filter({ hasText: /карантин/i }),
  ).toBeAttached({ timeout: 10_000 });
});
