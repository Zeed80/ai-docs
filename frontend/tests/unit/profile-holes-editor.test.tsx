import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ru from "@/messages/ru.json";

const correctSpec = vi.fn();
vi.mock("@/lib/studio-api", () => ({
  correctSpec: (...args: unknown[]) => correctSpec(...args),
}));

import ProfileHolesEditor from "@/components/cad/ProfileHolesEditor";
import type { SpecVerification } from "@/lib/studio-api";

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

// plate-1: пластина 80×50, отверстие 3 прочитано с y = 26 от нижней кромки
// (в спеке — от середины: 1), замер по листу — y = 32 (7,006 от середины).
const SPEC = {
  main_view: {
    profile: {
      shape: "rectangle",
      width_mm: 80,
      height_mm: 50,
      thickness_mm: 25,
      holes: [
        { center_x_mm: -22, center_y_mm: -2, diameter_mm: 5.5 },
        { center_x_mm: -1, center_y_mm: -6, diameter_mm: 11 },
        { center_x_mm: 12, center_y_mm: 1, diameter_mm: 6.6 },
      ],
    },
  },
};

const VERIFICATION: SpecVerification = {
  items: [
    {
      kind: "plate_hole",
      path: "main_view.profile.holes[2]",
      read: { center_x_mm: 12, center_y_mm: 1, diameter_mm: 6.6 },
      status: "refuted",
      measured: { center_x_mm: 12.007, center_y_mm: 7.006, diameter_mm: 6.698 },
      reason: "y 32.006 мм, прочитано 26",
      // Ø 6,70 против 6,6 — в допуске 0,3: замера у Ø быть не должно.
      tolerance_mm: { position: 0.5, diameter: 0.3 },
    },
  ],
  summary: { checked: 1, confirmed: 0, refuted: 1, unmeasurable: 0 },
};

describe("ProfileHolesEditor", () => {
  beforeEach(() =>
    correctSpec.mockReset().mockResolvedValue({ rebuild_task_id: null }),
  );

  it("shows plate coordinates from the sheet edges and the measured value to take", async () => {
    const onDone = vi.fn();
    render(
      <ProfileHolesEditor
        generationId="g1"
        spec={SPEC}
        verification={VERIFICATION}
        busy={false}
        onDone={onDone}
        onError={vi.fn()}
        t={t}
      />,
    );

    // Прочитанное — от кромок листа: x = 12 + 40 = 52, y = 1 + 25 = 26.
    const yInputs = screen.getAllByLabelText(
      "y от нижней кромки, мм",
    ) as HTMLInputElement[];
    expect(yInputs[2].value).toBe("26");
    expect(screen.getByText("замер: 32.01")).toBeTruthy();

    fireEvent.click(screen.getByText("взять замер"));
    expect(yInputs[2].value).toBe("32.01");

    fireEvent.click(screen.getByText("Только сохранить исправление"));
    await vi.waitFor(() => expect(correctSpec).toHaveBeenCalled());
    const [generation, correction, options] = correctSpec.mock.calls[0];
    expect(generation).toBe("g1");
    expect(options).toEqual({ rebuild: false });
    // Весь профиль, координаты — в системе спека (от середины).
    const profile = (correction as { profile: typeof SPEC.main_view.profile })
      .profile;
    expect(profile.width_mm).toBe(80);
    expect(profile.holes[2].center_y_mm).toBeCloseTo(7.006, 3);
    expect(profile.holes[0]).toEqual(SPEC.main_view.profile.holes[0]);
  });

  it("stays out of the way for a part without plan holes", () => {
    const { container } = render(
      <ProfileHolesEditor
        generationId="g1"
        spec={{ main_view: { outer: [{ diameter_mm: 30, length_mm: 50 }] } }}
        busy={false}
        onDone={vi.fn()}
        onError={vi.fn()}
        t={t}
      />,
    );
    expect(container.innerHTML).toBe("");
  });
});
