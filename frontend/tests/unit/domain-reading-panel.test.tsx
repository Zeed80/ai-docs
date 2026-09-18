import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DomainReadingPanel from "@/components/cad/DomainReadingPanel";
import ru from "@/messages/ru.json";

function t(key: string, values: Record<string, string | number> = {}): string {
  const template = key.split(".").reduce<unknown>(
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
          report: { skipped: [{ id: "w4", kind: "wall", reason: "no_height" }] },
        }}
      />,
    );
    expect(screen.getByText("План этажа — модель здания")).toBeTruthy();
    expect(screen.getByText(/стен построено 3 из 5/)).toBeTruthy();
    expect(screen.getByText("Исключено, не угадывается: 1")).toBeTruthy();
  });
});
