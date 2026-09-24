import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DomainReadingPanel from "@/components/cad/DomainReadingPanel";
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

describe("DomainReadingPanel — строительные и схемы (X5)", () => {
  it("показывает итог и исключённое", () => {
    render(
      <DomainReadingPanel
        t={t}
        reading={{
          domain: "construction",
          summary: "План этажа: стен построено 3 из 5",
          report: {
            skipped: [{ id: "w4", kind: "wall", reason: "no_height" }],
          },
        }}
      />,
    );
    expect(screen.getByText("План этажа — модель здания")).toBeTruthy();
    expect(screen.getByText(/стен построено 3 из 5/)).toBeTruthy();
    expect(screen.getByText("Исключено, не угадывается: 1")).toBeTruthy();
  });

  it("показывает отметки уровня, найденные по знакам листа", () => {
    render(
      <DomainReadingPanel
        t={t}
        reading={{
          domain: "construction",
          summary: "План этажа: стен построено 0 из 0",
          report: {},
          levels: { values: ["-1.800", "0.000"], places_found: 3 },
        }}
      />,
    );
    expect(
      screen.getByText(
        "Отметки уровня по знакам листа: -1.800 · 0.000 (знаков и рамок найдено 3)",
      ),
    ).toBeTruthy();
  });
  it("показывает масштаб и стены, измеренные по листу (Ф7)", () => {
    render(
      <DomainReadingPanel
        t={t}
        reading={{
          domain: "construction",
          summary: "План этажа",
          report: {
            measurement: {
              mm_per_px: 8.6,
              spans: 4,
              markers: 7,
              walls_measured: 49,
              openings_measured: 5,
              doors: 2,
              windows: 3,
            },
          },
        }}
      />,
    );
    expect(
      screen.getByText(
        "Замер по листу: масштаб 8.6 мм/px по 4 звеньям цепочки между осями (7 маркеров), стен измерено 49; проёмов 5 (дверей 2, окон 3)",
      ),
    ).toBeTruthy();
  });

  it("говорит, почему стены не измерены", () => {
    render(
      <DomainReadingPanel
        t={t}
        reading={{
          domain: "construction",
          summary: "План этажа",
          report: {
            measurement: {
              mm_per_px: null,
              reason: "осей на листе не найдено",
            },
          },
        }}
      />,
    );
    expect(
      screen.getByText("Стены по листу не измерены: осей на листе не найдено"),
    ).toBeTruthy();
  });
});
