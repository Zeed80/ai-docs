import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { LinkifiedText } from "@/components/chat/linkified-text";

afterEach(cleanup);

it("делает адрес ссылкой в новой вкладке без точки в конце предложения", () => {
  render(<LinkifiedText text="Каталог: https://example.com/cat.pdf. Готово" />);
  const link = screen.getByRole("link");
  expect(link.getAttribute("href")).toBe("https://example.com/cat.pdf");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  expect(document.body.textContent).toContain(". Готово");
});

it("markdown-ссылка показывает текст, путь приложения тоже ссылка", () => {
  render(
    <LinkifiedText text="Открой [счёт 42](/invoices/42) или [сайт](https://a.ru)" />,
  );
  const links = screen.getAllByRole("link");
  expect(links.map((l) => l.textContent)).toEqual(["счёт 42", "сайт"]);
  expect(links[0].getAttribute("href")).toBe("/invoices/42");
});

it("не превращает javascript: в ссылку", () => {
  render(<LinkifiedText text="[жми](javascript:alert(1)) просто текст" />);
  expect(screen.queryByRole("link")).toBeNull();
});

it("сноска [n] ведёт на прочитанную страницу, неизвестный номер остаётся текстом", () => {
  render(
    <LinkifiedText
      text="Напор 32 м [1], масса — [7]. Каталог: https://a.ru/x"
      sources={[{ n: 1, url: "https://hms.example/km", title: "Ливгидромаш", kind: "manufacturer" }]}
    />,
  );
  const footnote = screen.getByRole("link", { name: "Источник 1: Ливгидромаш" });
  expect(footnote.getAttribute("href")).toBe("https://hms.example/km");
  expect(footnote.getAttribute("title")).toContain("производитель");
  expect(document.body.textContent).toContain("[7]");
  expect(screen.getAllByRole("link")).toHaveLength(2);
});

it("без источников [1] остаётся текстом", () => {
  render(<LinkifiedText text="Напор 32 м [1]." />);
  expect(screen.queryByRole("link")).toBeNull();
});
