import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import AssemblyCompositionPanel from "@/components/cad/AssemblyCompositionPanel";
import ru from "@/messages/ru.json";

function t(key: string, values: Record<string, string | number> = {}): string {
  const template = key
    .split(".")
    .reduce<unknown>(
      (node, part) => (node as Record<string, unknown>)?.[part],
      ru.studio,
    );
  if (typeof template !== "string") throw new Error(`нет ключа ${key}`);
  return template.replace(/\{(\w+)\}/g, (_m, name) =>
    String(values[name] ?? `{${name}}`),
  );
}

describe("AssemblyCompositionPanel — состав сборки (X5)", () => {
  it("показывает связанные позиции и не прячет несвязанные", () => {
    render(
      <AssemblyCompositionPanel
        t={t}
        assembly={{
          positions: [1, 2, 20, 21],
          rows: 3,
          linked: [
            {
              position: 1,
              designation: "ТМ.0004.ХХ.101",
              name: "Корпус",
              quantity: 1,
            },
            {
              position: 2,
              designation: "ТМ.0004.ХХ.102",
              name: "Втулка",
              quantity: 2,
            },
          ],
          positions_without_row: [20, 21],
          rows_without_position: [3],
          duplicated_rows: [],
          link_rate: 0.5,
        }}
      />,
    );
    expect(screen.getByText("Корпус")).toBeTruthy();
    expect(
      screen.getByText(/Позиции без строки спецификации: 20, 21/),
    ).toBeTruthy();
    expect(
      screen.getByText(/Строки спецификации без позиции на чертеже: 3/),
    ).toBeTruthy();
    expect(screen.getByText(/3D сборки по чертежу не строится/)).toBeTruthy();
  });

  it("говорит, сколько позиций подтверждено номером на полке листа", () => {
    render(
      <AssemblyCompositionPanel
        t={t}
        assembly={{
          positions: [1, 2, 3],
          rows: 3,
          linked: [],
          positions_without_row: [],
          rows_without_position: [],
          duplicated_rows: [],
          link_rate: 1,
          sheet_positions: { shelves: 4, read: [1, 3, 9] },
        }}
      />,
    );
    expect(
      screen.getByText(
        /Полок выносок на листе: 4; номер на полке подтверждён у 2 из 3 позиций/,
      ),
    ).toBeTruthy();
  });

  it("ничего не рисует без прочитанного состава", () => {
    const { container } = render(<AssemblyCompositionPanel t={t} />);
    expect(container.innerHTML).toBe("");
  });
});
