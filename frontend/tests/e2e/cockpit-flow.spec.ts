import { expect, test, type BrowserContext } from "@playwright/test";

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

test("create case, upload quarantined document, and see audit timeline", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);

  // Mock ingest to return a suspicious document (ClamAV not available in E2E env)
  const mockDocId = "00000000-dead-beef-0000-000000000001";
  const mockCaseId = "00000000-cafe-babe-0000-000000000001";
  const title = `E2E quarantine ${Date.now()}`;

  await page.route("**/api/cases", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: mockCaseId,
          title,
          customer: "E2E customer",
          task_description: "Проверить безопасный upload flow.",
          status: "open",
          created_by: "system",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          documents_count: 0,
        }),
      });
    } else {
      await route.continue();
    }
  });

  await page.route(`**/api/cases/${mockCaseId}`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: mockCaseId,
          title,
          customer: "E2E customer",
          task_description: "Проверить безопасный upload flow.",
          status: "open",
          created_by: "system",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          documents_count: 1,
          documents: [
            {
              id: mockDocId,
              file_name: "payload.exe",
              status: "suspicious",
              doc_type: null,
              added_at: new Date().toISOString(),
            },
          ],
          timeline: [
            {
              id: "00000000-0000-0000-0000-000000000001",
              event_type: "case_created",
              actor: "system",
              summary: `Кейс создан: ${title}`,
              timestamp: new Date().toISOString(),
            },
            {
              id: "00000000-0000-0000-0000-000000000002",
              event_type: "document_quarantined",
              actor: "system",
              summary: "Документ добавлен: payload.exe",
              timestamp: new Date().toISOString(),
            },
          ],
          approval_gates: [],
        }),
      });
    } else {
      await route.continue();
    }
  });

  await page.route("**/api/documents/ingest", async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        id: mockDocId,
        file_name: "payload.exe",
        status: "suspicious",
      }),
    });
  });

  await page.route(`**/api/cases/${mockCaseId}/documents`, async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          case_id: mockCaseId,
          document_id: mockDocId,
          event_type: "document_quarantined",
        }),
      });
    } else {
      await route.continue();
    }
  });

  await page.goto("/");

  await page
    .getByPlaceholder("Название кейса: Вал Ø25 / счет Hoffmann")
    .fill(title);
  await page.getByPlaceholder("Заказчик").fill("E2E customer");
  await page
    .getByPlaceholder("Что нужно сделать технологу?")
    .fill("Проверить безопасный upload flow.");
  await page.getByRole("button", { name: "Создать кейс" }).click();

  await expect(page.getByRole("heading", { name: title })).toBeVisible();

  const payload = Buffer.from("MZ suspicious e2e payload");
  await page
    .locator("section")
    .filter({ hasText: "Добавить документ" })
    .locator('input[type="file"]')
    .setInputFiles({
      name: "payload.exe",
      mimeType: "application/octet-stream",
      buffer: payload,
    });
  await page.getByRole("button", { name: "Добавить документ" }).click();

  await expect(
    page.getByRole("heading", { name: "payload.exe" }),
  ).toBeVisible();
  await expect(page.getByText("suspicious").first()).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Process", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText("document_quarantined")).toBeVisible();
});

test("agent scenario creates approval gate and approval cockpit can approve it", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);

  const mockCaseId = "00000000-abcd-1234-0000-000000000002";
  const mockApprovalId = "00000000-ef01-2345-0000-000000000003";
  const title = `E2E approval ${Date.now()}`;

  // Track approval state for mock
  let approvalStatus = "pending";

  await page.route("**/api/cases", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: mockCaseId,
          title,
          customer: null,
          task_description: null,
          status: "open",
          created_by: "system",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          documents_count: 0,
        }),
      });
    } else {
      await route.continue();
    }
  });

  await page.route(`**/api/cases/${mockCaseId}`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: mockCaseId,
          title,
          customer: null,
          task_description: null,
          status: "open",
          created_by: "system",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          documents_count: 0,
          documents: [],
          timeline: [
            {
              id: "t1",
              event_type: "case_created",
              actor: "system",
              summary: `Кейс создан: ${title}`,
              timestamp: new Date().toISOString(),
            },
            {
              id: "t2",
              event_type: "approval_gate_created",
              actor: "agent",
              summary: "Требуется подтверждение: email.send.request_approval",
              timestamp: new Date().toISOString(),
            },
            ...(approvalStatus !== "pending"
              ? [
                  {
                    id: "t3",
                    event_type: "approval_gate_approved",
                    actor: "user",
                    summary: "Решение по approval: одобрено",
                    timestamp: new Date().toISOString(),
                  },
                ]
              : []),
          ],
          approval_gates: [
            {
              id: mockApprovalId,
              action_type: "email.send.request_approval",
              status: approvalStatus,
              requested_by: "agent",
              context: {},
              created_at: new Date().toISOString(),
              decided_at:
                approvalStatus !== "pending" ? new Date().toISOString() : null,
              decided_by: approvalStatus !== "pending" ? "user" : null,
            },
          ],
        }),
      });
    } else {
      await route.continue();
    }
  });

  await page.route(
    `**/api/cases/${mockCaseId}/approvals/${mockApprovalId}/decide`,
    async (route) => {
      approvalStatus = "approved";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          status: "approved",
          event_type: "approval_gate_approved",
        }),
      });
    },
  );

  await page.goto(`/cases/${mockCaseId}`);
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(
    page.getByText("email.send.request_approval", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("approval_gate_approved")).toBeVisible();
  await expect(page.getByText("approval_gate_created")).toBeVisible();
});
