import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import AssurancePanel from "@/components/cad/AssurancePanel";
import type { SpecVerification } from "@/lib/studio-api";
import ru from "@/messages/ru.json";

// Настоящий словарь: тест заодно держит, что ключи есть и подставляются.
function t(key: string, values: Record<string, string | number> = {}): string {
  const template = key.split(".").reduce<unknown>(
    (node, part) => (node as Record<string, unknown>)?.[part],
    ru.studio, // CadWorkspace: useTranslations("studio")
  );
  if (typeof template !== "string") throw new Error(`нет ключа ${key}`);
  return template.replace(/\{(\w+)\}/g, (_m, name) =>
    String(values[name] ?? `{${name}}`),
  );
}

const VERIFICATION: SpecVerification = {
  items: [
    {
      kind: "plate_hole",
      path: "main_view.profile.holes[0]",
      read: { center_x_mm: -22, center_y_mm: -2, diameter_mm: 5.5 },
      status: "confirmed",
      measured: { center_x_mm: -21.99, center_y_mm: -1.99, diameter_mm: 5.61 },
      reason: "",
    },
    {
      kind: "plate_hole",
      path: "main_view.profile.holes[2]",
      read: { center_x_mm: 12, center_y_mm: 1, diameter_mm: 6.8 },
      status: "refuted",
      measured: { center_x_mm: 12.01, center_y_mm: 7.01, diameter_mm: 6.7 },
      reason: "y 32.006 мм, прочитано 26",
    },
    {
      kind: "bolt_circle",
      path: "main_view.profile.hole_patterns[0]",
      read: { count: 6, bolt_circle_diameter_mm: 145 },
      status: "unmeasurable",
      measured: {},
      reason: "контур детали на листе не найден",
    },
  ],
  summary: { checked: 3, confirmed: 1, refuted: 1, unmeasurable: 1 },
};

describe("AssurancePanel — проверка прочитанного по листу", () => {
  it("shows the summary and names every refuted or unmeasurable element with its reason", () => {
    render(<AssurancePanel verification={VERIFICATION} t={t} />);

    expect(
      screen.getByText(
        "Прочитанное проверено по самому листу: подтверждено 1, опровергнуто 1, не измеримо 1",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("Отверстие 3: чтение опровергнуто замером по листу"),
    ).toBeTruthy();
    expect(screen.getByText(/y 32\.006 мм, прочитано 26/)).toBeTruthy();
    expect(
      screen.getByText("Окружность болтов 1: проверить по листу не удалось"),
    ).toBeTruthy();
    // Подтверждённое — только в сводке, поимённой строки нет.
    expect(screen.queryByText(/Отверстие 1:/)).toBeNull();
  });

  it("shows what was taken from the sheet and what waits for a person's decision", () => {
    const verification: SpecVerification = {
      items: [
        {
          kind: "shaft_step",
          path: "main_view.outer[5]",
          read: { diameter_mm: 35, length_mm: 80 },
          status: "confirmed",
          measured: { diameter_mm: 35.03, length_mm: 80.035 },
          reason: "принято по листу: length_mm 98 → 80",
          reconciled: { length_mm: { read: 98, adopted: 80 } },
        },
        {
          kind: "shaft_step",
          path: "main_view.outer[1]",
          read: { diameter_mm: 40, length_mm: 30 },
          status: "refuted",
          measured: { diameter_mm: 27.917, length_mm: 29.95 },
          reason: "ступень 2: Ø 27.917, прочитано 40",
        },
      ],
      summary: { checked: 2, confirmed: 1, refuted: 1, unmeasurable: 0 },
      reconciliation: [
        {
          kind: "shaft_step",
          path: "main_view.outer[5]",
          field: "length_mm",
          read: 98,
          measured: 80.035,
          action: "adopt",
          value: 80,
          reason: "замер 80.035 совпал с надписью «80» на листе",
        },
        {
          kind: "shaft_step",
          path: "main_view.outer[1]",
          field: "diameter_mm",
          read: 40,
          measured: 27.917,
          action: "ask_human",
          reason: "прочитанное 40 тоже есть на листе",
        },
      ],
    };
    render(<AssurancePanel verification={verification} t={t} />);

    expect(
      screen.getByText("Принято по листу: Ступень 6, длина 98 → 80"),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Нужно ваше решение: Ступень 2, Ø — прочитано 40, по листу 27.917",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/прочитанное 40 тоже есть на листе/)).toBeTruthy();
  });

  it("shows a shaft profile assembled from the sheet instead of the reading", () => {
    const verification: SpecVerification = {
      items: [
        {
          kind: "shaft_step",
          path: "main_view.outer[0]",
          read: { diameter_mm: 18, length_mm: 15 },
          status: "confirmed",
          measured: {},
          reason: "уступы и Ø вида объясняются надписями листа",
        },
      ],
      summary: { checked: 1, confirmed: 1, refuted: 0, unmeasurable: 0 },
      profile_adoption: {
        reason: "прочитанный Ø15.7×15 · Ø25×19 по листу не подтвердился",
        read: [
          [15.7, 15],
          [25, 19],
        ],
        value: [
          {
            diameter_mm: 18,
            length_mm: 15,
            thread: { designation: "M18" },
          },
          { diameter_mm: 25, length_mm: 19 },
        ],
      },
    };
    render(<AssurancePanel verification={verification} t={t} />);

    expect(
      screen.getByText("Профиль вала собран по листу: M18×15 · Ø25×19"),
    ).toBeTruthy();
    expect(screen.getByText(/по листу не подтвердился/)).toBeTruthy();
  });

  it("shows a keyway found on the sheet that the reader missed", () => {
    const verification: SpecVerification = {
      items: [
        {
          kind: "shaft_step",
          path: "main_view.outer[6]",
          read: { diameter_mm: 22, length_mm: 52 },
          status: "confirmed",
          measured: {},
          reason: "",
        },
      ],
      summary: { checked: 1, confirmed: 1, refuted: 0, unmeasurable: 0 },
      keyway_additions: [
        {
          step_index: 6,
          axial_start_mm: 150,
          length_mm: 25,
          width_mm: 6,
          depth_mm: 3.5,
          reason: "паз найден на листе, ридер его не выписал",
        },
      ],
    };
    render(<AssurancePanel verification={verification} t={t} />);

    expect(
      screen.getByText("Паз найден по листу: 150…175 мм, 6 × 3.5"),
    ).toBeTruthy();
    expect(screen.getByText(/ридер его не выписал/)).toBeTruthy();
  });

  it("renders nothing when there is nothing checked at all", () => {
    const { container } = render(<AssurancePanel t={t} />);
    expect(container.innerHTML).toBe("");
  });
});
