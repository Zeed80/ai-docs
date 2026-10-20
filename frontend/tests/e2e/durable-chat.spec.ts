import { expect, test } from "@playwright/test";

test("подтверждение восстанавливается после reload и разрешает только сохранённое действие", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const session = {id: "11111111-1111-4111-8111-111111111111", title: "Подтверждение", user_key: "test-user"};
  let approved = false;
  const decisions: unknown[] = [];
  let intakePosts = 0;
  const run = () => ({id: "run", session_id: session.id, work_order_id: "order", request_id: "request",
    status: approved ? "completed" : "blocked", result_message_id: approved ? "answer" : null});
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/ai/agent-config") return route.fulfill({json: {agent_name: "Света"}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [session]});
    if (path.endsWith("/messages")) return route.fulfill({json: [{id: "user", role: "user", content: "Отправь письмо", attachments: []}]});
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") intakePosts++;
      return route.fulfill({json: {run: run(), legacy: false}});
    }
    if (path.endsWith("/checkpoint")) return route.fulfill({json: {can_resume: !approved, attempt_id: "attempt", sha256: "a".repeat(64), confirmation: {tool: "email.send", args: {message_id: "draft-42"}}}});
    if (path.endsWith("/resume")) {
      decisions.push(route.request().postDataJSON()); approved = true;
      return route.fulfill({status: 202, json: run()});
    }
    if (path.endsWith("/events")) return route.fulfill({json: {items: approved && Number(url.searchParams.get("after")) < 2 ? [{sequence: 2, type: "chat.text", payload: {event: {type: "text", content: "Сохранённое действие завершено"}}}] : [], next_cursor: approved ? 2 : 0}});
    if (path === "/api/agent/chat-runs/run") return route.fulfill({json: run()});
    return route.fulfill({json: {}});
  });
  await page.goto("/assistant");
  await expect(page.getByRole("button", {name: "Разрешить действие"}).first()).toBeEnabled();
  expect(decisions).toHaveLength(0);
  await page.reload();
  const card = page.getByRole("region", {name: "Подтверждение сохранённого действия"}).first();
  await expect(card).toContainText("draft-42");
  await card.getByRole("button", {name: "Разрешить действие"}).click();
  await expect(page.getByText("Сохранённое действие завершено", {exact: true}).first()).toBeVisible();
  expect(decisions).toEqual([{attempt_id: "attempt", sha256: "a".repeat(64), approved: true}]);
  expect(intakePosts).toBe(0);
});

test("verified commit показывает отдельное продолжение и не повторяет действие", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const session = {id: "11111111-1111-4111-8111-111111111111", title: "Проверенный commit", user_key: "test-user"};
  const decisions: unknown[] = [];
  let continued = false;
  const run = () => ({id: "run", session_id: session.id, work_order_id: "order", request_id: "request",
    status: continued ? "completed" : "blocked", result_message_id: continued ? "answer" : null});
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/ai/agent-config") return route.fulfill({json: {agent_name: "Света"}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [session]});
    if (path.endsWith("/messages")) return route.fulfill({json: [{id: "user", role: "user", content: "Создай задачу", attachments: []}]});
    if (path === "/api/agent/chat-runs") return route.fulfill({json: {run: run(), legacy: false}});
    if (path.endsWith("/checkpoint")) return route.fulfill({json: continued ? {can_resume: false} : {
      can_resume: true, intent: "verified_commit", action_id: "action", attempt_id: "attempt", sha256: "b".repeat(64),
    }});
    if (path.endsWith("/resume")) {
      decisions.push(route.request().postDataJSON()); continued = true;
      return route.fulfill({status: 202, json: run()});
    }
    if (path.endsWith("/events")) return route.fulfill({json: {items: continued ? [{sequence: 2, type: "chat.text", payload: {event: {type: "text", content: "Продолжен безопасный хвост"}}}] : [], next_cursor: continued ? 2 : 0}});
    if (path === "/api/agent/chat-runs/run") return route.fulfill({json: run()});
    return route.fulfill({json: {}});
  });
  await page.goto("/assistant");
  const card = page.getByRole("region", {name: "Продолжение после проверенного commit"}).first();
  await expect(card).toContainText("не повтор действия");
  await card.getByRole("button", {name: "Продолжить после проверенного результата"}).click();
  await expect(page.getByText("Продолжен безопасный хвост", {exact: true}).first()).toBeVisible();
  expect(decisions).toEqual([{intent: "verified_commit", action_id: "action", attempt_id: "attempt", sha256: "b".repeat(64), approved: true}]);
});

test("HTTP-чат восстанавливает работу после reload без повторного POST", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const session = {id: "11111111-1111-4111-8111-111111111111", title: "Проверка восстановления",
    user_key: "test-user", created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z"};
  let submitted = false;
  let completed = false;
  let posts = 0;
  const run = () => ({id: "run", session_id: session.id, work_order_id: "order", request_id: "request",
    status: completed ? "completed" : "running", result_message_id: completed ? "answer" : null});
  const sockets: string[] = [];
  page.on("websocket", (socket) => sockets.push(socket.url()));
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", email: "test@invalid", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/ai/agent-config") return route.fulfill({json: {agent_name: "Света"}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [session]});
    if (path.endsWith("/messages")) return route.fulfill({json: submitted ? [{id: "user-message", role: "user", content: "Проверь задачу", attachments: []}] : []});
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") { posts++; submitted = true; return route.fulfill({status: 202, json: run()}); }
      return route.fulfill({json: {run: submitted ? run() : null, legacy: false}});
    }
    if (path.endsWith("/events")) return route.fulfill({json: {items: completed ? [{sequence: 1, type: "chat.text", payload: {event: {type: "text", content: "Проверка завершена"}}}] : [], next_cursor: completed ? 1 : 0}});
    if (path === "/api/agent/chat-runs/run") return route.fulfill({json: run()});
    return route.fulfill({json: {}});
  });
  await page.goto("/assistant");
  const input = page.getByRole("textbox", {name: "Сообщение Света"}).first();
  await expect(input).toBeEnabled();
  await input.fill("Проверь задачу");
  await input.press("Enter");
  await expect.poll(() => posts).toBe(1);
  await page.reload();
  await expect(page.getByRole("textbox", {name: "Сообщение Света"}).first()).toBeDisabled();
  completed = true;
  await expect(page.getByText("Проверка завершена", {exact: true}).first()).toBeVisible();
  await expect(page.getByRole("textbox", {name: "Сообщение Света"}).first()).toBeEnabled();
  expect(posts).toBe(1);
  expect(sockets.some((url) => url.includes("/ws/chat"))).toBe(false);
});

test("вопрос с вариантами: кнопки из истории отправляют точное значение", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const session = {id: "11111111-1111-4111-8111-111111111111", title: "Тёзки", user_key: "test-user"};
  const posted: string[] = [];
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/ai/agent-config") return route.fulfill({json: {agent_name: "Света"}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [session]});
    if (path.endsWith("/messages")) return route.fulfill({json: [
      {id: "user", role: "user", content: "Напиши письмо Эвал-Меди", attachments: []},
      {id: "answer", role: "assistant", content: "Какой «Эвал-Медь»?\n\n1. ООО (рекомендую)\n2. АО", attachments: [],
        metadata: {clarify: {question: "Какой «Эвал-Медь»?", recommended: true, options: [
          {label: "ООО «Эвал-Медь», ИНН 7702000051", value: "ООО «Эвал-Медь» (7702000051)"},
          {label: "АО «Эвал-Медь», ИНН 7702000052", value: "АО «Эвал-Медь» (7702000052)"},
        ]}}},
      ...posted.map((_, i) => ({id: `reply-${i}`, role: "user", content: "АО «Эвал-Медь» (7702000052)", attachments: []})),
    ]});
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") {
        posted.push(JSON.stringify(route.request().postDataJSON()));
        return route.fulfill({status: 202, json: {id: "run", session_id: session.id, work_order_id: "order", request_id: "r", status: "running", result_message_id: null}});
      }
      return route.fulfill({json: {run: null, legacy: false}});
    }
    if (path.endsWith("/events")) return route.fulfill({json: {items: [], next_cursor: 0}});
    return route.fulfill({json: {}});
  });
  await page.goto("/assistant");
  const groups = page.getByRole("group", {name: "Варианты ответа"});
  await expect(groups.first()).toBeVisible();
  const before = await groups.count();
  const group = groups.first();
  await expect(group.getByRole("button")).toHaveCount(2);
  await expect(group.getByRole("button").first()).toContainText("рекомендую");
  await group.getByRole("button", {name: /АО «Эвал-Медь»/}).click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toContain("АО «Эвал-Медь» (7702000052)");
  // The answered question shows no buttons any more (the page may hold a
  // second chat panel of its own).
  await expect(groups).toHaveCount(before - 1);
});

test("ссылка @ на счёт: подсказка с клавиатуры и точный id в запросе", async ({context, page}) => {
  await context.addCookies([{name: "access_token", value: "mock-only", domain: "127.0.0.1", path: "/"}]);
  const session = {id: "11111111-1111-4111-8111-111111111111", title: "Ссылки", user_key: "test-user"};
  const posted: Record<string, unknown>[] = [];
  const queries: string[] = [];
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/auth/me") return route.fulfill({json: {sub: "test-user", name: "Test", roles: ["admin"], groups: [], sections: []}});
    if (path === "/api/ai/agent-config") return route.fulfill({json: {agent_name: "Света"}});
    if (path === "/api/chat/sessions") return route.fulfill({json: [session]});
    if (path.endsWith("/messages")) return route.fulfill({json: []});
    if (path === "/api/agent/mentions") {
      queries.push(url.searchParams.get("q") ?? "");
      return route.fulfill({json: [
        {type: "invoice", id: "3b759084-86f5-4182-be2f-9613a2adac78", label: "EV-4901", hint: "ООО «Эвал-Медь»"},
        {type: "invoice", id: "4c4682ee-a41b-4e95-804f-3867c2de133e", label: "EV-4902", hint: "ООО «Эвал-Сталь»"},
      ]});
    }
    if (path === "/api/agent/chat-runs") {
      if (route.request().method() === "POST") {
        posted.push(route.request().postDataJSON());
        return route.fulfill({status: 202, json: {id: "run", session_id: session.id, work_order_id: "order", request_id: "r", status: "running", result_message_id: null}});
      }
      return route.fulfill({json: {run: null, legacy: false}});
    }
    if (path.endsWith("/events")) return route.fulfill({json: {items: [], next_cursor: 0}});
    return route.fulfill({json: {}});
  });
  await page.goto("/assistant");
  const input = page.getByRole("textbox", {name: "Сообщение Света"}).first();
  await expect(input).toBeEnabled();
  await input.pressSequentially("Что со счётом @EV");
  const list = page.getByRole("listbox", {name: "Ссылка на запись"}).first();
  await expect(list.getByRole("option")).toHaveCount(2);
  await input.press("ArrowDown");
  await input.press("ArrowUp");
  await input.press("Enter");
  await expect(input).toHaveValue("Что со счётом @счёт:EV-4901 ");
  await input.pressSequentially("?");
  await input.press("Enter");
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0].content).toBe("Что со счётом @счёт:EV-4901 ?");
  expect((posted[0].workspace_context as {references: unknown[]}).references).toEqual([
    {type: "invoice", id: "3b759084-86f5-4182-be2f-9613a2adac78", label: "EV-4901"},
  ]);
  expect(queries).toContain("EV");
});
