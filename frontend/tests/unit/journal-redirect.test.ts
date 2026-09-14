import { expect, it } from "vitest";
import { NextRequest } from "next/server";
import { middleware } from "../../middleware";

it("сохраняет идентификатор запуска при переходе на вход", () => {
  const path = "/work-orders/chat-journal?run_id=11111111-1111-4111-8111-111111111111";
  const response = middleware(new NextRequest(`https://workspace.invalid${path}`));
  const location = new URL(response.headers.get("location")!);
  expect(response.status).toBe(307);
  expect(location.origin).toBe("https://workspace.invalid");
  expect(location.pathname).toBe("/auth/login");
  expect(location.searchParams.get("next")).toBe(path);
});

it("параметры не заменяют адрес страницы входа", () => {
  const response = middleware(new NextRequest("https://workspace.invalid/work-orders/chat-journal?next=https%3A%2F%2Fexternal.invalid"));
  const location = new URL(response.headers.get("location")!);
  expect(location.origin).toBe("https://workspace.invalid");
  expect(location.searchParams.get("next")).toBe("/work-orders/chat-journal?next=https%3A%2F%2Fexternal.invalid");
});
