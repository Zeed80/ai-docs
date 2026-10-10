import { expect, test } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

// E46: a standing permission is built from the action's typed fields, shown
// in words and as the exact JSON, and never sent without an exact scope.
test("permission builder pins typed fields and shows the exact grant", async ({ context, page }) => {
  await setAuthCookie(context);
  await mockEmptyApi(page);
  const posted: unknown[] = [];
  await page.route("**/api/agent/delegations**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/actions"))
      return route.fulfill({
        json: {
          items: [
            {
              name: "invoices.approve",
              effect: "write",
              fields: [
                { name: "invoice_id", type: "string", format: "uuid", required: true },
                { name: "comment", type: "string", format: null, required: false },
              ],
            },
          ],
        },
      });
    if (route.request().method() === "POST") {
      posted.push(route.request().postDataJSON());
      return route.fulfill({ status: 201, json: {} });
    }
    return route.fulfill({ json: { items: [] } });
  });

  await page.goto("/settings/delegations");
  await expect(page.getByRole("option", { name: /invoices\.approve/ })).toBeAttached();
  await page.getByLabel("Действие").selectOption("invoices.approve");
  await page.getByLabel("Название").fill("Один счёт");
  await expect(page.getByLabel("Название")).toHaveValue("Один счёт");
  const submit = page.getByRole("button", { name: /Выдать разрешение/ });
  await expect(submit).toBeDisabled(); // no exact scope yet

  await page.getByLabel(/invoice_id/).fill("00000000-0000-0000-0000-000000000001");
  await expect(page.getByText(/только если invoice_id = "00000000-0000-0000-0000-000000000001"/)).toBeVisible();
  await expect(page.getByLabel("Итоговое разрешение")).toContainText('"invoice_id"');
  await expect(page.getByLabel("Итоговое разрешение")).not.toContainText('"comment"');
  await submit.click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({
    actions: ["invoices.approve"],
    constraints: { invoice_id: "00000000-0000-0000-0000-000000000001" },
  });
});
