import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActionJournal } from "@/components/chat/action-journal";
import { mutFetch } from "@/lib/auth";

vi.mock("@/lib/auth", () => ({mutFetch: vi.fn()}));
const fetcher = vi.mocked(mutFetch);
const action = {id: "action", tool: "email.send", status: "outcome_unknown", request_digest: "a".repeat(64), latest_observation: null};
const detail = {...action, request: {draft_id: "draft-42"}, result: null, result_digest: null};
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status});
beforeEach(() => fetcher.mockReset());
afterEach(cleanup);

it("показывает квитанцию отдельно от потерянного ответа без автоматического продолжения", async () => {
  fetcher.mockResolvedValueOnce(response({items: [action], next_offset: 1, work_order_status: "blocked"}))
    .mockResolvedValueOnce(response({...detail, recipient_receipt: {
      operation: "agent_control.task_propose", response: {id: "committed-task-42"},
      response_digest: "b".repeat(64), evidence_scope: "database_commit",
    }}));
  render(<ActionJournal runId="run" />);
  fireEvent.click(await screen.findByRole("button", {name: /email.send/}));
  await screen.findByRole("region", {name: "Квитанция получателя"});
  expect(screen.getByText(/committed-task-42/)).toBeInTheDocument();
  expect(screen.getByText("Результат не сохранён")).toBeInTheDocument();
  expect(screen.getByText(/Это не проверка текущего состояния/)).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
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
