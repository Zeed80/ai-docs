/**
 * Scenario 5: Proactive Follow-up
 * Flow: user opens /calendar, sees reminders with upcoming deadlines,
 * clicks "Follow-up ↗" → draft created → redirect to draft page.
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

const REMINDER_ID = "cccccccc-0000-4000-8000-000000000001";
const INV_ID = "dddddddd-0000-4000-8000-000000000001";
const DRAFT_ID = "eeeeeeee-0000-4000-8000-000000000001";

const calendarData = {
  events: [
    {
      id: "evt-1",
      title: "Срок оплаты: Счёт МС-101",
      event_date: new Date(Date.now() + 3 * 86400 * 1000).toISOString(),
      event_type: "due_date",
      entity_type: "invoice",
      entity_id: INV_ID,
      source: "invoice",
      created_at: "2026-05-01T10:00:00Z",
    },
    {
      id: "evt-2",
      title: "Доставка: Вал Ø25 от ИП Стальторг",
      event_date: new Date(Date.now() + 7 * 86400 * 1000).toISOString(),
      event_type: "delivery",
      entity_type: "invoice",
      entity_id: INV_ID,
      source: "invoice",
      created_at: "2026-05-01T10:00:00Z",
    },
  ],
  reminders: [
    {
      id: REMINDER_ID,
      entity_type: "invoice",
      entity_id: INV_ID,
      remind_at: new Date(Date.now() + 3 * 86400 * 1000).toISOString(),
      message: "Напомнить об оплате счёта МС-101 (ООО Металл-Сервис, 48 500 ₽)",
      is_sent: false,
    },
  ],
};

async function mockApi(page: Page) {
  let reminderSent = false;

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/calendar/upcoming") {
      return route.fulfill({
        json: {
          ...calendarData,
          reminders: reminderSent ? [] : calendarData.reminders,
        },
      });
    }
    if (
      url.pathname ===
        `/api/calendar/reminders/${REMINDER_ID}/generate-followup` &&
      request.method() === "POST"
    ) {
      return route.fulfill({ json: { draft_id: DRAFT_ID } });
    }
    if (
      url.pathname === `/api/calendar/reminders/${REMINDER_ID}/mark-sent` &&
      request.method() === "POST"
    ) {
      reminderSent = true;
      return route.fulfill({ json: { ok: true } });
    }
    if (url.pathname === `/api/email/drafts/${DRAFT_ID}`) {
      return route.fulfill({
        json: {
          id: DRAFT_ID,
          subject: "Re: Оплата счёта МС-101",
          body: "Уважаемые коллеги, напоминаем о предстоящем сроке оплаты...",
          status: "draft",
          executed: false,
          draft_data: {
            to: "supplier@example.com",
            subject: "Re: Оплата счёта МС-101",
          },
          created_at: "2026-05-17T12:00:00Z",
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
          created_at: "2026-05-17T12:00:00Z",
          updated_at: "2026-05-17T12:00:00Z",
        },
      });
    }
    if (url.pathname.startsWith("/api/chat/sessions/"))
      return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("Scenario 5: calendar shows upcoming events and reminders", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/calendar");

  // Events visible
  await expect(page.getByText("Срок оплаты: Счёт МС-101")).toBeVisible();
  await expect(
    page.getByText("Доставка: Вал Ø25 от ИП Стальторг"),
  ).toBeVisible();

  // Reminder visible
  await expect(
    page.getByText("Напомнить об оплате счёта МС-101"),
  ).toBeVisible();

  // Follow-up button visible (entity_type=invoice)
  await expect(page.getByRole("button", { name: "Follow-up ↗" })).toBeVisible();
});

test("Scenario 5: generate follow-up draft opens draft page", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);

  // Intercept window.open to capture the URL instead of opening new tab
  let openedUrl = "";
  await page.exposeFunction("recordOpen", (url: string) => {
    openedUrl = url;
  });
  await page.addInitScript(() => {
    window.open = (url?: string | URL) => {
      (
        window as Window &
          typeof globalThis & { recordOpen: (u: string) => void }
      ).recordOpen(String(url ?? ""));
      return null;
    };
  });

  await page.goto("/calendar");
  await expect(
    page.getByText("Напомнить об оплате счёта МС-101"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Follow-up ↗" }).click();

  // Wait for the URL to be recorded
  await page.waitForFunction(() => {
    const w = window as Window &
      typeof globalThis & { recordOpen?: (u: string) => void };
    return typeof w.recordOpen === "function";
  });

  // Give a moment for the async handler to fire
  await page.waitForTimeout(500);
  expect(openedUrl).toContain(DRAFT_ID);
});

test("Scenario 5: mark reminder as done removes it from list", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/calendar");

  await expect(
    page.getByText("Напомнить об оплате счёта МС-101"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Выполнено" }).click();

  await expect(page.getByText("Напомнить об оплате счёта МС-101")).toBeHidden();
});
