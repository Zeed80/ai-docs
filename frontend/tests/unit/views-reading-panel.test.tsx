import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/studio/CadModelViewer", () => ({ default: () => null }));

import ViewsReadingPanel from "@/components/cad/ViewsReadingPanel";
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

describe("ViewsReadingPanel — метод «по видам листа»", () => {
  it("показывает основу тела и надписи, которых на теле нет", () => {
    render(
      <ViewsReadingPanel
        t={t}
        reading={{
          method: "views",
          ok: true,
          profile: { kind: "extrude", thickness_mm: 3 },
          features: [{}, {}],
          coverage: { explained: ["90", "100", "Ø10"], missing: ["Ø16"], share: 0.75 },
        }}
      />,
    );
    expect(screen.getByText(/Выдавливание контура вида на 3 мм/)).toBeTruthy();
    expect(screen.getByText("Надписи листа на теле: 3 из 4")).toBeTruthy();
    expect(screen.getByText(/Нет на теле .*Ø16/)).toBeTruthy();
  });

  it("при отказе показывает причину, а не пустоту", () => {
    render(
      <ViewsReadingPanel
        t={t}
        reading={{ method: "views", ok: false, reason: "толщина не найдена" }}
      />,
    );
    expect(screen.getByText(/Тело не построено: толщина не найдена/)).toBeTruthy();
  });
});
