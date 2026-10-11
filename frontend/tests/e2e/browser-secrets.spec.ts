/**
 * «Настройки → Браузер» (T3): пароль добавляется без возврата значения,
 * отзыв — только после подтверждения.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test browser-secrets --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

type Secret = {
  id: string;
  label: string;
  origin: string;
  created_at: string;
  revoked_at: string | null;
};

async function setup(page: Page, posts: unknown[]) {
  await mockEmptyApi(page);
  const secrets: Secret[] = [];
  await page.route("**/api/browser-secrets**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "POST" && url.pathname.endsWith("/revoke")) {
      const id = url.pathname.split("/").at(-2);
      const s = secrets.find((x) => x.id === id)!;
      s.revoked_at = "2026-10-12T01:00:00Z";
      posts.push({ revoke: id });
      return route.fulfill({ json: s });
    }
    if (req.method() === "POST") {
      const body = req.postDataJSON() as {
        label: string;
        origin: string;
        value: string;
      };
      posts.push(body);
      const row = {
        id: `s${secrets.length + 1}`,
        label: body.label,
        origin: body.origin,
        created_at: "2026-10-12T00:00:00Z",
        revoked_at: null,
      };
      secrets.unshift(row);
      return route.fulfill({ status: 201, json: row });
    }
    return route.fulfill({ json: secrets });
  });
}

test("a password is stored without coming back and revoked after a confirmation", async ({
  page,
  context,
}) => {
  const posts: unknown[] = [];
  await setAuthCookie(context);
  await setup(page, posts);
  await page.goto("/settings/browser");
  await expect(page.getByText("Паролей пока нет.")).toBeVisible();

  await page.getByLabel("Название").fill("Кабинет поставщика");
  await page.getByLabel("Сайт").fill("https://portal.example.ru");
  await page.getByLabel("Пароль", { exact: true }).fill("s3cret");
  await page.getByRole("button", { name: "Сохранить" }).click();
  await expect(page.getByText("Кабинет поставщика")).toBeVisible();
  await expect(page.getByLabel("Пароль", { exact: true })).toHaveValue("");
  await expect(page.getByText("s3cret")).toHaveCount(0);

  const revoke = page.getByRole("button", {
    name: "Отозвать Кабинет поставщика",
  });
  page.once("dialog", (d) => d.dismiss());
  await revoke.click();
  expect(posts).toHaveLength(1);
  page.once("dialog", (d) => {
    expect(d.message()).toContain("по уже выданным грантам");
    void d.accept();
  });
  await revoke.click();
  await expect(page.getByText("Отозван", { exact: true })).toBeVisible();
  expect(posts[1]).toEqual({ revoke: "s1" });
});

test("the browser secrets page fits a 320 px screen", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto("/settings/browser");
  await expect(page.getByRole("button", { name: "Сохранить" })).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
