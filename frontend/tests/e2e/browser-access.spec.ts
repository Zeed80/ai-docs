/**
 * Поручение → «Доступ браузера» (W3): автоматический доступ на чтение виден
 * и отзывается; доступ к действиям на сайтах выдаётся набором с хостами.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test browser-access --project=chromium
 */

import { expect, test, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const ID = "44444444-4444-4444-8444-444444444444";
const ORDER = {
  id: ID,
  objective: "Найти паспорт насоса",
  status: "running",
  display_status: "running",
  available_actions: ["cancel", "pause"],
  priority: 50,
  risk_level: "low",
  plan_revision: 1,
  created_at: "2026-10-29T08:00:00Z",
  updated_at: "2026-10-29T08:00:00Z",
};

test("the read grant is shown and an action grant is given with hosts", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockEmptyApi(page);
  const posts: Record<string, unknown>[] = [];
  let grants: Record<string, unknown>[] = [
    {
      id: "g-read",
      granted_by: "auto:read",
      automatic: true,
      actions: ["browser_fetch", "desktop_open"],
      allowed_hosts: ["*"],
      secret_ids: [],
      max_actions: 500,
      used_actions: 12,
      expires_at: "2026-10-29T12:00:00Z",
      revoked_at: null,
      reason: "Чтение сайтов",
      active: true,
    },
  ];
  await page.route("**/api/approvals/pending**", (route: Route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/work-orders**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname.endsWith("/computer-grants") && req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, unknown>;
      posts.push(body);
      grants = [
        {
          ...body,
          id: "g-act",
          automatic: false,
          granted_by: "me",
          used_actions: 0,
          expires_at: "2026-10-29T13:00:00Z",
          revoked_at: null,
          active: true,
        },
        ...grants,
      ];
      return route.fulfill({ status: 201, json: { id: "g-act" } });
    }
    if (url.pathname.endsWith("/revoke")) {
      grants = grants.map((g) =>
        g.id === "g-read" ? { ...g, active: false, revoked_at: "x" } : g,
      );
      posts.push({ revoked: url.pathname.split("/").at(-2) });
      return route.fulfill({ json: { ok: true } });
    }
    if (url.pathname.endsWith("/computer-grants"))
      return route.fulfill({ json: grants });
    if (url.pathname.endsWith("/metrics"))
      return route.fulfill({
        json: { window_hours: 24, status_counts: {}, step_durations: {} },
      });
    if (url.pathname.endsWith("/plan"))
      return route.fulfill({ json: { steps: [] } });
    if (
      url.pathname.endsWith("/events") ||
      url.pathname.endsWith("/tool-calls")
    )
      return route.fulfill({ json: [] });
    if (url.pathname === "/api/work-orders")
      return route.fulfill({ json: [ORDER] });
    return route.fulfill({ json: {} });
  });

  await page.goto("/work-orders");
  await page.getByText("Найти паспорт насоса").first().click();
  await expect(
    page.getByText("Чтение сайтов — выдано автоматически"),
  ).toBeVisible();
  await expect(
    page.getByText("12 из 500 действий", { exact: false }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Выдать доступ" }).click();
  await page
    .getByPlaceholder("portal.supplier.ru")
    .fill("portal.example, b2b.example");
  await page.getByRole("button", { name: "Выдать", exact: true }).click();
  await expect(page.getByText("Действия на сайтах")).toBeVisible();
  expect(posts[0].allowed_hosts).toEqual(["portal.example", "b2b.example"]);
  expect(posts[0].actions).toEqual(
    expect.arrayContaining(["desktop_click", "desktop_open"]),
  );
  expect(posts[0].actions).not.toContain("desktop_fill_secret");

  page.once("dialog", (d) => d.accept());
  await page
    .getByRole("listitem")
    .filter({ hasText: "выдано автоматически" })
    .getByRole("button", { name: "Отозвать" })
    .click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toEqual({ revoked: "g-read" });
});
