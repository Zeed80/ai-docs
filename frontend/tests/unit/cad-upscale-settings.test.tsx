import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({ apiFetch: vi.fn(), mutFetch: vi.fn() }));

import UpscaleSettingsCard from "@/components/cad/UpscaleSettingsCard";
import { apiFetch, mutFetch } from "@/lib/auth";
import ru from "@/messages/ru.json";

const LIMITS = {
  min_line_px: [2, 12],
  max_factor: [2, 8],
  timeout_s: [60, 3600],
  min_agreement: [0.5, 0.95],
};

function config(cad_upscale: Record<string, unknown>) {
  return new Response(
    JSON.stringify({
      auto_verify_enabled: true,
      cad_upscale: {
        enabled: true,
        enabled_source: "environment",
        env_enabled: true,
        min_line_px: 4.5,
        max_factor: 8,
        timeout_s: 600,
        min_agreement: 0.72,
        limits: LIMITS,
        ...cad_upscale,
      },
    }),
    { status: 200 },
  );
}

function renderCard() {
  return render(
    <NextIntlClientProvider locale="ru" messages={ru}>
      <UpscaleSettingsCard />
    </NextIntlClientProvider>,
  );
}

describe("UpscaleSettingsCard — улучшение грубого листа", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(mutFetch).mockReset();
  });

  it("выключение галочки сохраняет решение в настройках", async () => {
    vi.mocked(apiFetch).mockResolvedValue(config({}));
    vi.mocked(mutFetch).mockResolvedValue(
      config({ enabled: false, enabled_source: "settings" }),
    );
    renderCard();

    const box = await screen.findByRole("checkbox");
    expect(box).toBeChecked();
    fireEvent.click(box);

    await waitFor(() => expect(box).not.toBeChecked());
    const [, init] = vi.mocked(mutFetch).mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({ cad_upscale_enabled: false });
    expect(screen.getByText(ru.cad.upscale.reset_environment)).toBeInTheDocument();
  });

  it("параметры вне пределов не отправляются", async () => {
    vi.mocked(apiFetch).mockResolvedValue(config({}));
    renderCard();

    const agreement = await screen.findByLabelText(
      ru.cad.upscale.min_agreement_label,
      { exact: false },
    );
    fireEvent.change(agreement, { target: { value: "0.1" } });

    const save = screen.getByRole("button", { name: ru.cad.upscale.save });
    expect(save).toBeDisabled();
    expect(agreement).toHaveAttribute("aria-invalid", "true");
    expect(mutFetch).not.toHaveBeenCalled();
  });

  it("сбой сервера виден, а не выглядит как успех", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response("boom", { status: 500 }));
    renderCard();

    expect(await screen.findByRole("alert")).toHaveTextContent("500");
  });
});
