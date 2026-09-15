import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActionJournal } from "@/components/chat/action-journal";
import { mutFetch } from "@/lib/auth";

vi.mock("@/lib/auth", () => ({mutFetch: vi.fn()}));
const fetcher = vi.mocked(mutFetch);
const action = {id: "action", tool: "email.send", status: "outcome_unknown", request_digest: "a".repeat(64), latest_observation: null};
const detail = {...action, request: {draft_id: "draft-42"}, result: null, result_digest: null};
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status});
const receipt = {operation: "agent_control.task_propose", response: {id: "committed-task-42"}, response_digest: "b".repeat(64), evidence_scope: "database_commit"};
const verification = (status: "matched" | "changed" | "missing" | "inconclusive" = "matched") => ({
  action_id: action.id, status, observed_at: "2026-09-15T12:00:00Z", scope: "agent_task_content_snapshot", can_replay: false, can_resume: false,
});
beforeEach(() => fetcher.mockReset());
afterEach(cleanup);

it("показывает квитанцию отдельно от потерянного ответа без автоматического продолжения", async () => {
  fetcher.mockResolvedValueOnce(response({items: [action], next_offset: 1, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, recipient_receipt: receipt}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByRole("region", {name: "Квитанция получателя"});
  expect(screen.getByText(/committed-task-42/)).toBeInTheDocument();
  expect(screen.getByText("Результат не сохранён")).toBeInTheDocument();
  expect(screen.getByText(/Это не проверка текущего состояния/)).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
});

async function openReceipt(actionOverride: Partial<typeof action> = {}) {
  const currentAction = {...action, ...actionOverride};
  fetcher.mockResolvedValueOnce(response({items: [currentAction], next_offset: 1, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, ...actionOverride, recipient_receipt: receipt}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: new RegExp(currentAction.tool)}));
  await screen.findByRole("region", {name: "Квитанция получателя"});
}

it("показывает matched как отдельную текущую сверку только через GET", async () => {
  await openReceipt();
  fetcher.mockResolvedValueOnce(response(verification()));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText("Статус: Совпадает на момент сверки");
  expect(screen.getByText("Время наблюдения: 2026-09-15T12:00:00Z")).toBeInTheDocument();
  expect(screen.getByText("Область сверки: agent_task_content_snapshot")).toBeInTheDocument();
  const calls = fetcher.mock.calls.filter(([path]) => String(path).endsWith("/verification"));
  expect(calls).toEqual([["/api/agent/chat-runs/run/actions/action/verification", expect.objectContaining({method: "GET"})]]);
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
  expect(fetcher.mock.calls.some(([path]) => String(path).includes("/resume"))).toBe(false);
});

it.each([
  ["missing", "Объект больше не найден"],
  ["changed", "Изменилось с момента квитанции"],
] as const)("показывает %s без кнопки исполнения", async (status, label) => {
  await openReceipt();
  fetcher.mockResolvedValueOnce(response(verification(status)));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText(`Статус: ${label}`);
  expect(screen.queryByRole("button", {name: /Возобновить|Разрешить|Повторить действие/})).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
});

it("очищает matched при повторной сверке и игнорирует поздний первый ответ", async () => {
  await openReceipt();
  fetcher.mockResolvedValueOnce(response(verification()));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText("Статус: Совпадает на момент сверки");
  let resolve!: (value: Response) => void;
  fetcher.mockReturnValueOnce(new Promise<Response>((done) => {resolve = done;}));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  expect(screen.queryByText("Статус: Совпадает на момент сверки")).not.toBeInTheDocument();
  resolve(response(verification("changed")));
  await screen.findByText("Статус: Изменилось с момента квитанции");
  expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith("/verification"))).toHaveLength(2);
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
});

it("не переносит запоздалую сверку к другой карточке", async () => {
  const second = {...action, id: "second", tool: "files.write", request_digest: "c".repeat(64)};
  let resolve!: (value: Response) => void;
  fetcher.mockResolvedValueOnce(response({items: [action, second], next_offset: 2, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, recipient_receipt: receipt}))
    .mockReturnValueOnce(new Promise<Response>((done) => {resolve = done;}))
    .mockResolvedValueOnce(response({...detail, ...second, request: {path: "second.txt"}, recipient_receipt: {...receipt, response: {id: "second-task"}}}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByRole("button", {name: "Проверить текущее состояние"});
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  fireEvent.click(screen.getByRole("button", {name: /files.write/}));
  await screen.findByText(/second.txt/);
  resolve(response(verification()));
  await waitFor(() => expect(screen.queryByText("Статус: Совпадает на момент сверки")).not.toBeInTheDocument());
});

it("очищает прошлый verdict при отказе доступа после успеха", async () => {
  await openReceipt();
  fetcher.mockResolvedValueOnce(response(verification()));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText("Статус: Совпадает на момент сверки");
  fetcher.mockResolvedValueOnce(response({}, 403));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText("Текущий доступ администратора для сверки отсутствует.");
  expect(screen.queryByText("Статус: Совпадает на момент сверки")).not.toBeInTheDocument();
});

it.each([
  [409, "Квитанция или запись действия нарушает проверку целостности."],
  [500, "Не удалось проверить текущее состояние: Error: HTTP 500"],
])("не оставляет matched после HTTP %i", async (status, message) => {
  await openReceipt();
  fetcher.mockResolvedValueOnce(response(verification()));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText("Статус: Совпадает на момент сверки");
  fetcher.mockResolvedValueOnce(response({}, status));
  fireEvent.click(screen.getByRole("button", {name: "Проверить текущее состояние"}));
  await screen.findByText(message);
  expect(screen.queryByText("Статус: Совпадает на момент сверки")).not.toBeInTheDocument();
});

async function open(status = "outcome_unknown", workStatus = "blocked") {
  fetcher.mockResolvedValueOnce(response({items: [{...action, status}], next_offset: 1, work_order_status: workStatus}))
    .mockResolvedValueOnce(response({...detail, status}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByText(/draft-42/);
}

it("показывает неизвестный исход без кнопки исполнения и без автоматической записи", async () => {
  await open();
  expect(screen.getByText("Результат не сохранён")).toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Сохранить наблюдение"})).toBeDisabled();
  expect(screen.queryByRole("button", {name: /Разрешить|Возобновить|Повторить действие/})).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
});

it.each([["planned", "blocked"], ["result_recorded", "blocked"], ["outcome_unknown", "running"]])("не предлагает наблюдение для %s / %s", async (status, workStatus) => {
  await open(status, workStatus);
  expect(screen.queryByLabelText("Обоснование")).not.toBeInTheDocument();
});

it("повторяет точное наблюдение после потери ответа и не снимает блокировку", async () => {
  await open();
  fireEvent.change(screen.getByLabelText("Обоснование"), {target: {value: "Проверен журнал получателя"}});
  fireEvent.change(screen.getByLabelText("Ссылка или идентификатор свидетельства"), {target: {value: "log:42"}});
  fetcher.mockRejectedValueOnce(new TypeError("network"));
  fireEvent.click(screen.getByRole("button", {name: "Сохранить наблюдение"}));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Обоснование")).toBeDisabled();
  fetcher.mockImplementationOnce(async (_path, init) => response(JSON.parse(String(init?.body))));
  fireEvent.click(screen.getByRole("button", {name: "Повторить сохранение наблюдения"}));
  await screen.findByText("Последнее наблюдение — не проверено");
  const posts = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(posts).toHaveLength(2);
  expect(posts[0][0]).toBe("/api/agent/chat-runs/run/actions/action/observations");
  expect(posts[0][1]?.body).toBe(posts[1][1]?.body);
  expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({request_digest: action.request_digest, outcome: "inconclusive", evidence_reference: "log:42"});
  expect(screen.getByText("Состояние работы: blocked")).toBeInTheDocument();
});

it("загружает предыдущие наблюдения страницами, не дублирует sequence и не пишет", async () => {
  const latest = {sequence: 40, request_id: "latest", actor: "owner", outcome: "observed", note: "Последнее", evidence_reference: "manual:latest", verified: false, can_replay: false};
  const previous = Array.from({length: 20}, (_, index) => ({sequence: index === 19 ? 19 : index + 1, request_id: `old-${index}`, actor: "owner", outcome: "inconclusive", note: index === 0 ? "<img src=x onerror=alert(1)>" : `Ранее ${index}`, evidence_reference: `manual:${index}`, verified: false, can_replay: false}));
  fetcher.mockResolvedValueOnce(response({items: [{...action, latest_observation: latest}], next_offset: 1, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, latest_observation: latest}))
    .mockResolvedValueOnce(response({items: previous, next_cursor: 19}))
    .mockResolvedValueOnce(response({items: [{sequence: 40, request_id: "latest", actor: "owner", outcome: "observed", note: "Последнее", evidence_reference: "manual:latest", verified: false, can_replay: false}], next_cursor: 40}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByText("Последнее наблюдение — не проверено");
  fireEvent.click(screen.getByRole("button", {name: "Показать предыдущие наблюдения"}));
  await screen.findByRole("region", {name: "Предыдущие наблюдения"});
  expect(screen.getAllByText("Ранее 19")).toHaveLength(1);
  expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name: "Показать ещё предыдущие наблюдения"}));
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => String(path).includes("/observations?"))).toHaveLength(2));
  expect(screen.getAllByText("Последнее")).toHaveLength(1);
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
});

it("игнорирует запоздалую страницу истории после смены действия и очищает её ошибку", async () => {
  const latest = {sequence: 3, request_id: "latest", outcome: "observed", note: "Последнее", evidence_reference: "manual:latest", verified: false, can_replay: false};
  const second = {...action, id: "second", tool: "files.write", latest_observation: latest};
  let resolve!: (value: Response) => void;
  fetcher.mockResolvedValueOnce(response({items: [{...action, latest_observation: latest}, second], next_offset: 2, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, latest_observation: latest}))
    .mockReturnValueOnce(new Promise<Response>((done) => {resolve = done;}))
    .mockResolvedValueOnce(response({...detail, ...second, request: {path: "second.txt"}, latest_observation: latest}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByRole("button", {name: "Показать предыдущие наблюдения"});
  fireEvent.click(screen.getByRole("button", {name: "Показать предыдущие наблюдения"}));
  fireEvent.click(screen.getByRole("button", {name: /files.write/}));
  await screen.findByText(/second.txt/);
  resolve(response({items: [{sequence: 1, request_id: "old", outcome: "observed", note: "Чужая запоздалая страница", evidence_reference: "manual:old", verified: false, can_replay: false}], next_cursor: 1}));
  await waitFor(() => expect(screen.queryByText("Чужая запоздалая страница")).not.toBeInTheDocument());
  expect(screen.queryByText(/Не удалось прочитать предыдущие наблюдения/)).not.toBeInTheDocument();
});

it("очищает ошибку истории при смене действия", async () => {
  const latest = {sequence: 3, request_id: "latest", outcome: "observed", note: "Последнее", evidence_reference: "manual:latest", verified: false, can_replay: false};
  const second = {...action, id: "second", tool: "files.write", latest_observation: latest};
  fetcher.mockResolvedValueOnce(response({items: [{...action, latest_observation: latest}, second], next_offset: 2, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, latest_observation: latest}))
    .mockResolvedValueOnce(response({}, 500))
    .mockResolvedValueOnce(response({...detail, ...second, request: {path: "second.txt"}, latest_observation: latest}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  fireEvent.click(await screen.findByRole("button", {name: "Показать предыдущие наблюдения"}));
  await screen.findByText(/Не удалось прочитать предыдущие наблюдения/);
  fireEvent.click(screen.getByRole("button", {name: /files.write/}));
  await screen.findByText(/second.txt/);
  expect(screen.queryByText(/Не удалось прочитать предыдущие наблюдения/)).not.toBeInTheDocument();
});

it("при отказе доступа не показывает чужие данные или форму", async () => {
  fetcher.mockResolvedValueOnce(response({}, 404));
  render(<ActionJournal runId="foreign" />);
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("Обоснование")).not.toBeInTheDocument();
});

it("при нарушенной целостности не показывает форму", async () => {
  fetcher.mockResolvedValueOnce(response({items: [action], next_offset: 1, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({}, 409));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("Обоснование")).not.toBeInTheDocument();
});

it("игнорирует запоздалые детали ранее выбранного действия", async () => {
  let resolve!: (value: Response) => void;
  fetcher.mockResolvedValueOnce(response({items: [action, {...action, id: "second", tool: "files.write"}], next_offset: 2, work_order_status: "blocked"}))
    .mockReturnValueOnce(new Promise<Response>((r) => {resolve = r;}))
    .mockResolvedValueOnce(response({...detail, id: "second", request: {path: "second.txt"}}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  fireEvent.click(screen.getByRole("button", {name: /files.write/}));
  await screen.findByText(/second.txt/);
  resolve(response(detail));
  await waitFor(() => expect(screen.queryByText(/draft-42/)).not.toBeInTheDocument());
});

it("пагинирует журнал и не выдаёт пустую страницу за отсутствие эффекта", async () => {
  fetcher.mockResolvedValueOnce(response({items: Array.from({length: 20}, (_, n) => ({...action, id: String(n)})), next_offset: 20, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({items: [], next_offset: 20, work_order_status: "blocked"}));
  render(<ActionJournal runId="run" />);
  await screen.findAllByRole("button", {name: /email.send/});
  fireEvent.click(screen.getByRole("button", {name: "Далее"}));
  await screen.findByText("На этой странице нет записанных действий.");
  expect(fetcher).toHaveBeenLastCalledWith(expect.stringContaining("offset=20&limit=20"), expect.anything());
  expect(screen.getByText(/Отсутствие записи не доказывает/)).toBeInTheDocument();
});
