import { describe, expect, it } from "vitest";
import {
  EMPTY_FILTERS,
  filtersActive,
  matchesFilters,
  type ModelFilters,
} from "@/components/models/picker/ModelFilterBar";
import type { CatalogModel } from "@/lib/models/types";

function model(over: Partial<CatalogModel> = {}): CatalogModel {
  return {
    key: "openrouter/x",
    provider: "openrouter",
    provider_model: "vendor/x",
    status: "active",
    modalities: ["text"],
    local_only: false,
    thinking_supported: false,
    thinking_enabled: false,
    thinking_levels: [],
    thinking_level_default: null,
    preferred_instance: null,
    quality_score: 0,
    speed_score: 0,
    vram_gb_estimate: null,
    availability: "available",
    max_context_tokens: null,
    supports_tool_calling: false,
    supports_structured_output: false,
    cost_per_1k_input: null,
    cost_per_1k_output: null,
    notes: null,
    ...over,
  };
}

const with_ = (over: Partial<ModelFilters>): ModelFilters => ({
  ...EMPTY_FILTERS,
  ...over,
});

describe("matchesFilters", () => {
  it("пустые фильтры пропускают всё", () => {
    expect(matchesFilters(model(), EMPTY_FILTERS)).toBe(true);
    expect(filtersActive(EMPTY_FILTERS)).toBe(false);
  });

  describe("цена", () => {
    it("бесплатной считается нулевая цена, а не отсутствующая", () => {
      const free = model({ cost_per_1k_input: 0, cost_per_1k_output: 0 });
      const unknown = model({ cost_per_1k_input: null });
      const paid = model({
        cost_per_1k_input: 0.003,
        cost_per_1k_output: 0.015,
      });
      const f = with_({ price: "free" });
      expect(matchesFilters(free, f)).toBe(true);
      // Самая важная строка файла: «цену не прислали» — не «бесплатно».
      expect(matchesFilters(unknown, f)).toBe(false);
      expect(matchesFilters(paid, f)).toBe(false);
    });

    it("суффикс :free у OpenRouter — тоже бесплатно", () => {
      const m = model({ provider_model: "google/gemma-4-26b:free" });
      expect(matchesFilters(m, with_({ price: "free" }))).toBe(true);
    });

    it("порог считается за 1M токенов, хотя хранится за 1000", () => {
      // $0.90 за 1M — под порогом $1, хотя в каталоге это 0.0009.
      const cheap = model({ cost_per_1k_input: 0.0009 });
      const dear = model({ cost_per_1k_input: 0.003 });
      expect(matchesFilters(cheap, with_({ price: "lte1" }))).toBe(true);
      expect(matchesFilters(dear, with_({ price: "lte1" }))).toBe(false);
      expect(matchesFilters(dear, with_({ price: "lte5" }))).toBe(true);
    });

    it("модель без цены не проходит порог", () => {
      expect(matchesFilters(model(), with_({ price: "lte5" }))).toBe(false);
    });
  });

  describe("размер", () => {
    it("отбирает по оценке VRAM", () => {
      expect(
        matchesFilters(
          model({ vram_gb_estimate: 5.5 }),
          with_({ size: "lte8" }),
        ),
      ).toBe(true);
      expect(
        matchesFilters(
          model({ vram_gb_estimate: 18 }),
          with_({ size: "lte8" }),
        ),
      ).toBe(false);
      expect(
        matchesFilters(
          model({ vram_gb_estimate: 18 }),
          with_({ size: "lte32" }),
        ),
      ).toBe(true);
    });

    it("неизвестный вес не выдаётся за подходящий", () => {
      expect(matchesFilters(model(), with_({ size: "lte32" }))).toBe(false);
    });
  });

  describe("контекст", () => {
    it("порог включающий", () => {
      const m = model({ max_context_tokens: 131072 });
      expect(matchesFilters(m, with_({ context: "128k" }))).toBe(true);
      expect(matchesFilters(m, with_({ context: "1m" }))).toBe(false);
      expect(
        matchesFilters(
          model({ max_context_tokens: 32768 }),
          with_({ context: "32k" }),
        ),
      ).toBe(true);
    });

    it("неизвестный контекст не проходит", () => {
      expect(matchesFilters(model(), with_({ context: "32k" }))).toBe(false);
    });
  });

  describe("умения", () => {
    it("инструменты, зрение и рассуждение — три независимых условия", () => {
      const full = model({
        supports_tool_calling: true,
        modalities: ["text", "vision"],
        thinking_supported: true,
      });
      expect(
        matchesFilters(
          full,
          with_({ tools: true, vision: true, thinking: true }),
        ),
      ).toBe(true);
      expect(matchesFilters(model(), with_({ tools: true }))).toBe(false);
      expect(matchesFilters(model(), with_({ vision: true }))).toBe(false);
      expect(matchesFilters(model(), with_({ thinking: true }))).toBe(false);
    });
  });

  it("условия складываются, а не заменяют друг друга", () => {
    const m = model({
      cost_per_1k_input: 0,
      cost_per_1k_output: 0,
      max_context_tokens: 500_000,
      supports_tool_calling: true,
    });
    const f = with_({ price: "free", context: "128k", tools: true });
    expect(matchesFilters(m, f)).toBe(true);
    expect(matchesFilters({ ...m, supports_tool_calling: false }, f)).toBe(
      false,
    );
  });
});

describe("filtersActive", () => {
  it("любое непустое условие делает панель активной", () => {
    expect(filtersActive(with_({ price: "free" }))).toBe(true);
    expect(filtersActive(with_({ size: "lte8" }))).toBe(true);
    expect(filtersActive(with_({ context: "1m" }))).toBe(true);
    expect(filtersActive(with_({ vision: true }))).toBe(true);
  });
});
