/**
 * «Настройки → Процедуры»: предложение агента рядом с действующим текстом,
 * одобрение с правкой отправляет исправленный текст, отклонённые скрыты.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test agent-procedures --project=chromium
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

type Row = Record<string, unknown> & { id: string };

function procedure(overrides: Record<string, unknown>): Row {
  return {
    id: "p1",
    name: "mail",
    description: "Письма поставщикам",
    text: "Новая версия: подпись отдела снабжения.",
    status: "proposed",
    origin: "agent",
    reason: "владелец попросил запомнить подпись",
    source_work_order_id: null,
    proposed_by: "agent",
    decided_by: null,
    decided_at: null,
    pinned: false,
    uses: 0,
    last_used_at: null,
    created_at: "2026-10-20T08:00:00Z",
    current_text: "Действующая версия: подпись без отдела.",
    ...overrides,
  };
}

async function setup(page: Page, posts: { path: string; body: unknown }[]) {
  await mockEmptyApi(page);
  let rows = [
    procedure({}),
    procedure({
      id: "p2",
      name: "reminders",
      description: "Напоминания",
      text: "Напоминание — только по просьбе.",
      status: "active",
      current_text: null,
      uses: 3,
      last_used_at: "2026-10-19T08:00:00Z",
    }),
    procedure({
      id: "p3",
      name: "old-idea",
      status: "rejected",
      current_text: null,
    }),
  ];
  await page.route("**/api/agent/procedures-registry", (route: Route) =>
    route.fulfill({ json: rows }),
  );
  await page.route(
    "**/api/agent/procedures-registry/*/*",
    async (route: Route) => {
      const path = new URL(route.request().url()).pathname;
      const body = route.request().postDataJSON();
      posts.push({ path, body });
      if (path.endsWith("/p1/decide")) {
        rows = rows.map((r) =>
          r.id === "p1"
            ? { ...r, status: "active", text: body.text, current_text: null }
            : r,
        );
      }
      if (path.endsWith("/p2/pin")) {
        rows = rows.map((r) => (r.id === "p2" ? { ...r, pinned: true } : r));
      }
      return route.fulfill({ json: { ok: true } });
    },
  );
}

test("a proposal is approved with an edit, pinning and history work", async ({
  page,
  context,
}) => {
  const posts: { path: string; body: unknown }[] = [];
  await setAuthCookie(context);
  await setup(page, posts);
  await page.goto("/settings/procedures");

  const mail = page.getByRole("region", { name: "Процедура mail" });
  await expect(mail.getByText("Ждёт решения")).toBeVisible();
  await expect(
    mail.getByText("Действующая версия: подпись без отдела."),
  ).toBeVisible();
  await expect(
    mail.getByText("Новая версия: подпись отдела снабжения."),
  ).toBeVisible();
  await expect(page.getByText("Ждут решения: 1")).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Процедура old-idea" }),
  ).toHaveCount(0);

  await mail.getByRole("button", { name: "Править" }).click();
  await mail
    .getByLabel("Текст процедуры")
    .fill("Подпись: отдел снабжения, тел. 101.");
  await mail.getByRole("button", { name: "Одобрить с правкой" }).click();
  await expect(mail.getByText("Действует")).toBeVisible();
  expect(posts[0]).toEqual({
    path: "/api/agent/procedures-registry/p1/decide",
    body: { approve: true, text: "Подпись: отдел снабжения, тел. 101." },
  });

  const reminders = page.getByRole("region", { name: "Процедура reminders" });
  await expect(reminders.getByText("Прочитана 3 раз")).toBeVisible();
  await reminders.getByRole("button", { name: "Закрепить" }).click();
  await expect(reminders.getByText("Закреплена")).toBeVisible();
  expect(posts[1]).toEqual({
    path: "/api/agent/procedures-registry/p2/pin",
    body: { pinned: true },
  });

  await page.getByLabel("Показать отклонённые и прежние версии").check();
  await expect(
    page.getByRole("region", { name: "Процедура old-idea" }),
  ).toBeVisible();
});
