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

  it("renders nothing when there is nothing checked at all", () => {
    const { container } = render(<AssurancePanel t={t} />);
    expect(container.innerHTML).toBe("");
  });
});
