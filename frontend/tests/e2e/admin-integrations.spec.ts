/**
 * «Администрирование → Интеграции»: страница открывается со всеми четырьмя
 * OAuth-провайдерами (раньше Яндекс и Mail.ru роняли её целиком), а почта
 * компании — это только домен и ссылка на вебмейл, без Mailcow.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test admin-integrations --project=chromium
 */

import { expect, test, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

test("integrations render every OAuth provider and the company mail settings", async ({
  page,
  context,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const puts: unknown[] = [];

  await setAuthCookie(context);
  await mockEmptyApi(page);
  await page.route("**/api/admin/integrations/oauth-apps", (route: Route) =>
    route.fulfill({
      json: ["google", "microsoft", "yandex", "mailru"].map((provider) => ({
        provider,
        client_id: null,
        client_secret_set: false,
        client_secret_hint: "",
        redirect_uri: null,
        configured: false,
      })),
    }),
  );
  await page.route(
    "**/api/admin/integrations/mail-server",
    async (route: Route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON() as Record<string, unknown>;
        puts.push(body);
        return route.fulfill({ json: body });
      }
      return route.fulfill({ json: { mail_domain: null, webmail_url: null } });
    },
  );

  await page.goto("/admin/integrations");
  await expect(page.getByText("Почта компании")).toBeVisible();
  await expect(page.getByText("Яндекс.Почта")).toBeVisible();
  await expect(page.getByText("Mail.ru", { exact: true })).toBeVisible();
  await expect(page.getByText(/Mailcow/)).toHaveCount(0);

  const mail = page.locator("div.rounded-lg", {
    has: page.getByRole("heading", { name: "Почта компании" }),
  });
  await mail.getByPlaceholder("example.com", { exact: true }).fill("ptsai.ru");
  await mail.getByRole("button", { name: "Сохранить", exact: true }).click();
  await expect(page.getByText("Сохранено")).toBeVisible();
  expect(puts).toEqual([{ mail_domain: "ptsai.ru", webmail_url: "" }]);
  expect(errors).toEqual([]);
});
