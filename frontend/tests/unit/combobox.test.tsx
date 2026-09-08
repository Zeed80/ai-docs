import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  Combobox,
  type ComboboxItem,
} from "@/components/ui/primitives/Combobox";

const ITEMS: ComboboxItem<string>[] = [
  { key: "a", group: "Доступно", search: "qwen3.5:9b", value: "qwen3.5:9b" },
  { key: "b", group: "Доступно", search: "qwen3.8:27b", value: "qwen3.8:27b" },
  {
    key: "c",
    group: "Требует внимания",
    search: "gemma4:e4b",
    value: "gemma4:e4b",
    disabled: true,
  },
];

function Harness({
  onChange = vi.fn(),
  items = ITEMS,
  filters,
  footer,
  onOpen,
}: {
  onChange?: (key: string) => void;
  items?: ComboboxItem<string>[];
  filters?: React.ReactNode;
  footer?: React.ReactNode;
  onOpen?: () => void;
}) {
  const [value, setValue] = useState<string | null>("a");
  return (
    // Карточка секции в разделе моделей объявлена overflow-hidden — именно она
    // и обрезала список. Тест держит эту обёртку, чтобы регрессия была видна.
    <div data-testid="clipping-card" style={{ overflow: "hidden" }}>
      <Combobox
        items={items}
        value={value}
        onChange={(k) => {
          setValue(k);
          onChange(k);
        }}
        buttonLabel={<span>{value ?? "не назначена"}</span>}
        renderItem={(item) => <span>{item.value}</span>}
        filters={filters}
        footer={footer}
        onOpen={onOpen}
      />
    </div>
  );
}

const open = () =>
  fireEvent.click(screen.getByRole("button", { expanded: false }));

describe("Combobox", () => {
  it("список рендерится порталом, а не внутри обрезающей карточки", () => {
    render(<Harness />);
    open();
    const list = screen.getByRole("listbox");
    const card = screen.getByTestId("clipping-card");
    // Суть исправления: пока список был потомком карточки, её overflow-hidden
    // резал его по нижней границе секции.
    expect(card.contains(list)).toBe(false);
    expect(document.body.contains(list)).toBe(true);
  });

  it("закрытый список ничего не рендерит", () => {
    render(<Harness />);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("поиск фильтрует строки", () => {
    render(<Harness />);
    open();
    fireEvent.change(screen.getByPlaceholderText("Поиск…"), {
      target: { value: "27b" },
    });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("option")).toHaveTextContent("qwen3.8:27b");
  });

  it("пустой результат объясняется, а не показывается пустотой", () => {
    render(<Harness />);
    open();
    fireEvent.change(screen.getByPlaceholderText("Поиск…"), {
      target: { value: "такого нет" },
    });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("Ничего не найдено")).toBeInTheDocument();
  });

  it("выбор закрывает список и отдаёт ключ", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    open();
    fireEvent.click(screen.getByRole("option", { name: "qwen3.8:27b" }));
    expect(onChange).toHaveBeenCalledWith("b");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("непригодная строка видна и объяснена, но не выбирается", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    open();
    const disabled = screen.getByRole("option", { name: "gemma4:e4b" });
    expect(disabled).toBeDisabled();
    fireEvent.click(disabled);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("группы идут в объявленном порядке", () => {
    render(<Harness />);
    open();
    const headers = within(screen.getByRole("listbox")).getAllByText(
      /Доступно|Требует внимания/,
    );
    expect(headers.map((h) => h.textContent)).toEqual([
      "Доступно",
      "Требует внимания",
    ]);
  });

  it("Escape закрывает список", () => {
    render(<Harness />);
    open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("клик вне списка закрывает его, клик внутри — нет", () => {
    render(<Harness />);
    open();
    fireEvent.mouseDown(screen.getByRole("listbox"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("фильтры и подпись показываются внутри списка", () => {
    render(
      <Harness
        filters={<button type="button">бесплатные</button>}
        footer="Показано 2 из 431"
      />,
    );
    open();
    expect(screen.getByText("бесплатные")).toBeInTheDocument();
    expect(screen.getByText("Показано 2 из 431")).toBeInTheDocument();
  });
});

describe("ленивое раскрытие", () => {
  it("не зовёт onOpen, пока список не раскрыли", () => {
    const onOpen = vi.fn();
    render(<Harness onOpen={onOpen} />);

    expect(onOpen).not.toHaveBeenCalled();
  });

  it("зовёт onOpen при раскрытии — и не зовёт при закрытии", () => {
    const onOpen = vi.fn();
    render(<Harness onOpen={onOpen} />);
    const toggle = () =>
      fireEvent.click(screen.getByRole("button", { name: /не назначена|a/ }));

    toggle();
    expect(onOpen).toHaveBeenCalledTimes(1);

    // Закрытие — не повод считать пригодность заново.
    toggle();
    expect(onOpen).toHaveBeenCalledTimes(1);

    toggle();
    expect(onOpen).toHaveBeenCalledTimes(2);
  });
});
