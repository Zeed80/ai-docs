import { expect, test } from "@playwright/test";

test("create case, upload quarantined document, and see audit timeline", async ({ page }) => {
  await page.goto("/");

  const title = `E2E quarantine ${Date.now()}`;
  await page.getByPlaceholder("Название кейса: Вал Ø25 / счет Hoffmann").fill(title);
  await page.getByPlaceholder("Заказчик").fill("E2E customer");
  await page.getByPlaceholder("Что нужно сделать технологу?").fill("Проверить безопасный upload flow.");
  await page.getByRole("button", { name: "Создать кейс" }).click();

  await expect(page.getByRole("heading", { name: title })).toBeVisible();

  const payload = Buffer.from("MZ suspicious e2e payload");
  await page.locator('input[type="file"]').setInputFiles({
    name: "payload.exe",
    mimeType: "application/octet-stream",
    buffer: payload,
  });
  await page.getByRole("button", { name: "Добавить документ" }).click();

  await expect(page.getByRole("heading", { name: "payload.exe" })).toBeVisible();
  await expect(page.getByText("suspicious").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Process", exact: true })).toBeDisabled();
  await expect(page.getByText("suspicious").first()).toBeVisible();
  await expect(page.getByText("document_quarantined")).toBeVisible();
});

test("agent scenario creates approval gate and approval cockpit can approve it", async ({ page, request }) => {
  const title = `E2E approval ${Date.now()}`;
  const caseResponse = await request.post("http://127.0.0.1:18000/api/cases", {
    data: { title },
  });
  expect(caseResponse.ok()).toBeTruthy();
  const created = await caseResponse.json();

  const scenarioResponse = await request.post(
    "http://127.0.0.1:18000/api/agent/scenarios/draft_email/run",
    {
      data: {
        case_id: created.id,
        draft_id: "e2e-draft",
        requested_tools: ["email.draft", "email.send.request_approval"],
      },
    },
  );
  expect(scenarioResponse.ok()).toBeTruthy();
  const scenario = await scenarioResponse.json();
  expect(scenario.approval_gates).toHaveLength(1);

  await page.goto(`/cases/${created.id}`);
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(page.getByText("email.send.request_approval", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("approval_gate_approved")).toBeVisible();
  await expect(page.getByText("approval_gate_created")).toBeVisible();
});
