/**
 * Чтение письма: iframe, блокировка внешних картинок, мобильная раскладка,
 * объединённые «Входящие».
 *
 * Три вещи здесь нельзя проверить ничем, кроме браузера: iframe с
 * sandbox под CSP (высота меряется родителем), реальная загрузка
 * удалённой картинки (её либо запрашивают, либо нет) и раскладка на узком
 * экране.
 */
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

const THREAD_ID = "aaaaaaaa-0001-4000-8000-000000000001";
const MESSAGE_ID = "bbbbbbbb-0001-4000-8000-000000000001";
const TRACKER = "https://tracker.example.invalid/pixel.gif";

const message = {
  id: MESSAGE_ID,
  thread_id: THREAD_ID,
  message_id_header: "<m1@example.com>",
  mailbox: "svetlana",
  from_address: "sales@postavshik.example",
  to_addresses: ["svetlana@example.com"],
  cc_addresses: [],
  subject: "Коммерческое предложение",
  body_text: "Здравствуйте! Направляем предложение.",
  body_text_derived: false,
  body_html: null,
  // Как это приходит после санитайзера: удалённая картинка обезврежена.
  body_html_sanitized:
    '<p style="color:#111">Здравствуйте! Направляем предложение.</p>' +
    `<img data-blocked-src="${TRACKER}" width="1" height="1">` +
    "<p>" + "Длинная строка письма. ".repeat(200) + "</p>",
  sent_at: "2026-08-20T09:00:00Z",
  received_at: "2026-08-20T09:00:00Z",
  has_attachments: false,
  attachment_count: 0,
  attachments_meta: null,
  attachments: [],
  is_inbound: true,
  is_read: false,
  is_starred: false,
  folder: "inbox",
  snippet: "Здравствуйте!",
  references: null,
  reply_to: null,
  headers_meta: null,
  images_trusted: false,
  derived_invoices: [],
  triage: null,
  created_at: "2026-08-20T09:00:00Z",
};

const thread = {
  id: THREAD_ID,
  subject: "Коммерческое предложение",
  mailbox: "svetlana",
  message_count: 1,
  last_message_at: "2026-08-20T09:00:00Z",
  is_read: false,
  is_starred: false,
  has_attachments: false,
  folder: "inbox",
  last_snippet: "Здравствуйте!",
  unread_count: 1,
  labels: [],
  sender: "sales@postavshik.example",
  messages: [message],
};

async function setAuthCookie(context: BrowserContext) {
  await context.addCookies([
    { name: "access_token", value: "e2e-token", domain: "127.0.0.1", path: "/" },
  ]);
}

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const p = url.pathname;

    if (p === "/api/email/mailboxes")
      return route.fulfill({
        json: [{
          name: "svetlana", display_name: "Света", is_personal: false,
          thread_count: 1, message_count: 1, unread_count: 1,
          last_sync_at: new Date().toISOString(), sync_error: null,
        }],
      });
    if (p === "/api/email/threads" && request.method() === "GET")
      return route.fulfill({
        json: { items: [thread], total: 1, next_cursor: null },
      });
    if (p === `/api/email/threads/${THREAD_ID}`)
      return route.fulfill({ json: thread });
    if (p === "/api/email/labels") return route.fulfill({ json: [] });
    if (p === "/api/email/drafts") return route.fulfill({ json: [] });
    if (p === "/api/email/folder-counts") return route.fulfill({ json: [] });
    if (p === "/api/inbox")
      return route.fulfill({
        json: {
          items: [
            {
              kind: "email", id: MESSAGE_ID, title: "Коммерческое предложение",
              subtitle: "sales@postavshik.example",
              at: "2026-08-20T09:00:00Z", url: `/email/${THREAD_ID}`,
              unread: true, severity: null, badge: "Письмо",
            },
            {
              kind: "document", id: "dddddddd-0001-4000-8000-000000000009",
              title: "Счёт № 102111", subtitle: "на проверке",
              at: "2026-08-20T08:00:00Z", url: "/documents",
              unread: false, severity: null, badge: "Документ",
            },
          ],
          total: 2, counts: { email: 1, document: 1, anomaly: 0 },
        },
      });

    if (p === "/api/auth/me")
      return route.fulfill({
        json: {
          sub: "e2e", email: "e2e@example.local", name: "E2E",
          preferred_username: "e2e", roles: ["admin"], groups: [],
        },
      });
    if (p === "/api/dashboard/feed")
      return route.fulfill({ json: { total: 0, items: [] } });
    if (p === "/api/quarantine/count") return route.fulfill({ json: { count: 0 } });
    if (p === "/api/notifications/unread-count")
      return route.fulfill({ json: { count: 0 } });
    if (p === "/api/ai/agent-config") return route.fulfill({ json: {} });
    if (p === "/api/chat/sessions" && request.method() === "GET")
      return route.fulfill({ json: [] });
    if (p.startsWith("/api/chat/sessions")) return route.fulfill({ json: [] });

    return route.fulfill({ json: {} });
  });
}

test("письмо рендерится в iframe и подстраивает высоту под содержимое", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/email");

  await page.getByText("Коммерческое предложение").first().click();

  const frame = page.locator("iframe[srcdoc]");
  await expect(frame).toBeVisible({ timeout: 10_000 });

  // Ф5.3 — фрейм был зафиксирован на 360 px, и длинное письмо скроллилось
  // внутри маленькой коробки внутри скролла страницы.
  await expect
    .poll(async () => (await frame.boundingBox())?.height ?? 0, {
      timeout: 10_000,
    })
    .toBeGreaterThan(400);

  // Тема письма не должна перекрашиваться нами: белая подложка, тёмный текст.
  const bodyColor = await frame.contentFrame().locator("body").evaluate(
    (el) => getComputedStyle(el).backgroundColor,
  );
  expect(bodyColor).toBe("rgb(255, 255, 255)");
});

test("удалённая картинка не загружается, пока её не разрешили", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);

  const trackerHits: string[] = [];
  await page.route("**/tracker.example.invalid/**", async (route: Route) => {
    trackerHits.push(route.request().url());
    await route.abort();
  });

  await page.goto("/email");
  await page.getByText("Коммерческое предложение").first().click();
  await expect(page.locator("iframe[srcdoc]")).toBeVisible({ timeout: 10_000 });

  await expect(page.getByText(/Изображения заблокированы/)).toBeVisible();
  await page.waitForTimeout(1000);
  expect(trackerHits).toEqual([]);

  // А после явного согласия картинка получает настоящий адрес: блокировка —
  // это выбор, а не запрет. (Сам запрос уходит уже из iframe, и перехват
  // сетевого слоя для sandbox-фрейма ненадёжен, поэтому проверяем разметку.)
  await page.getByRole("button", { name: /Показать/ }).first().click();
  await expect
    .poll(
      async () =>
        await page
          .locator("iframe[srcdoc]")
          .contentFrame()
          .locator("img")
          .first()
          .getAttribute("src"),
      { timeout: 10_000 },
    )
    .toBe(TRACKER);
});

test("на телефоне тред заменяет список и есть возврат назад", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/email");

  await page.getByText("Коммерческое предложение").first().click();

  // Ф7.2 — трёхпанельная раскладка на телефоне непригодна: тред занимает
  // экран целиком, и «назад» единственный выход.
  const back = page.getByRole("button", { name: "Назад к списку" });
  await expect(back).toBeVisible({ timeout: 10_000 });
  await back.click();
  await expect(page.getByText("Коммерческое предложение").first()).toBeVisible();
});

test("объединённые «Входящие» показывают письма и документы в одной ленте", async ({
  page,
  context,
}) => {
  await setAuthCookie(context);
  await mockApi(page);
  await page.goto("/inbox");

  await expect(page.getByText("Коммерческое предложение")).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText("Счёт № 102111")).toBeVisible();
});
