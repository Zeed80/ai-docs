/**
 * T3: владелец видит переданную сессию браузера, вводит код и возвращает её.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test browser-handoff --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

// 4×3 grey PNG: the picture only has to load and take clicks.
const PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAEklEQVR4nGNoaGhgwA0YRqVxAgCfYwz5i9mz4wAAAABJRU5ErkJggg==";
const WO = "11111111-1111-1111-1111-111111111111";

async function setup(page: Page, ops: Record<string, unknown>[]) {
  await mockEmptyApi(page);
  let handoff: { reason: string } | null = { reason: "Нужен код из SMS" };
  await page.route("**/api/computer-use/sessions?**", (route: Route) =>
    route.fulfill({
      json: {
        sessions: [
          { session_id: "s-1", url: "https://bank.example/mfa", handoff },
        ],
      },
    }),
  );
  await page.route(
    "**/api/computer-use/sessions/s-1/human",
    async (route: Route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      if (body.op !== "view") ops.push(body);
      if (body.op === "return") handoff = null;
      if (body.op === "take") handoff = { reason: "Владелец взял сессию сам" };
      return route.fulfill({
        json: {
          ok: true,
          url: "https://bank.example/mfa",
          title: "Подтверждение входа",
          handoff,
          outside_allowlist: false,
          viewport: { width: 1366, height: 900 },
          screenshot_b64: PNG,
        },
      });
    },
  );
}

test("the owner types the code into the handed-off session and returns it", async ({
  page,
  context,
}) => {
  const ops: Record<string, unknown>[] = [];
  await setAuthCookie(context);
  await setup(page, ops);
  await page.goto(`/work-orders/browser?work_order_id=${WO}&session=s-1`);
  await expect(page.getByText(/Сессия у вас: Нужен код из SMS/)).toBeVisible();

  const shot = page.getByRole("img", {
    name: "Страница в браузере сотрудника",
  });
  const box = (await shot.boundingBox())!;
  await shot.click({ position: { x: box.width / 2, y: box.height / 2 } });
  await page.getByLabel("Текст для страницы").fill("482913");
  await page.getByRole("button", { name: "Ввести" }).click();
  await expect(page.getByLabel("Текст для страницы")).toHaveValue("");
  await page.getByRole("button", { name: "Enter" }).click();
  await page.getByRole("button", { name: "Вернуть сотруднику" }).click();
  await expect(
    page.getByText("Сессия у сотрудника — вы только смотрите."),
  ).toBeVisible();

  expect(ops.map((o) => o.op)).toEqual(["click", "type", "key", "return"]);
  expect(Math.abs((ops[0].x as number) - 683)).toBeLessThanOrEqual(2);
  expect(Math.abs((ops[0].y as number) - 450)).toBeLessThanOrEqual(2);
  expect(ops[1]).toEqual({ work_order_id: WO, op: "type", text: "482913" });
});

test("the handoff screen fits a 320 px screen", async ({ page, context }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await setAuthCookie(context);
  await setup(page, []);
  await page.goto(`/work-orders/browser?work_order_id=${WO}&session=s-1`);
  await expect(
    page.getByRole("button", { name: "Вернуть сотруднику" }),
  ).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test("the picture refreshes every second while the owner holds the session", async ({
  page,
  context,
}) => {
  const ops: Record<string, unknown>[] = [];
  let views = 0;
  await setAuthCookie(context);
  await setup(page, ops);
  page.on("request", (request) => {
    if (
      request.url().endsWith("/sessions/s-1/human") &&
      request.postDataJSON()?.op === "view"
    )
      views += 1;
  });
  await page.goto(`/work-orders/browser?work_order_id=${WO}&session=s-1`);
  await expect(page.getByText(/Сессия у вас/)).toBeVisible();
  const before = views;
  await page.waitForTimeout(2500);
  // Every 3 s it would be at most one more picture in 2.5 s.
  expect(views - before).toBeGreaterThanOrEqual(2);
});
