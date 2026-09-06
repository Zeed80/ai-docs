"use client";

import type { CatalogModel } from "@/lib/models/types";

/**
 * Фильтры списка моделей.
 *
 * Появились из-за облака: у OpenRouter каталог — сотни моделей, и поиска по
 * имени мало. Имя не отвечает на вопросы «что здесь бесплатное», «что влезет
 * в мою память», «что умеет инструменты» — а выбирают обычно именно по ним.
 */
export interface ModelFilters {
  /** Цена за 1M входных токенов; для локальных провайдеров не применяется. */
  price: "any" | "free" | "lte1" | "lte5";
  /** Оценка VRAM; для облачных провайдеров не применяется. */
  size: "any" | "lte8" | "lte16" | "lte32";
  context: "any" | "32k" | "128k" | "1m";
  tools: boolean;
  vision: boolean;
  thinking: boolean;
}

export const EMPTY_FILTERS: ModelFilters = {
  price: "any",
  size: "any",
  context: "any",
  tools: false,
  vision: false,
  thinking: false,
};

export function filtersActive(f: ModelFilters): boolean {
  return (
    f.price !== "any" ||
    f.size !== "any" ||
    f.context !== "any" ||
    f.tools ||
    f.vision ||
    f.thinking
  );
}

/**
 * Бесплатна ли модель.
 *
 * Ноль в цене — это ответ «бесплатно», а `null` — «цена неизвестна»: их нельзя
 * смешивать, иначе фильтр «бесплатные» покажет всё, для чего провайдер не
 * прислал прайс. Суффикс `:free` — соглашение OpenRouter, по нему такие модели
 * и отличают.
 */
function isFree(m: CatalogModel): boolean {
  if (m.provider_model.endsWith(":free")) return true;
  return m.cost_per_1k_input === 0 && m.cost_per_1k_output === 0;
}

const CONTEXT_MIN: Record<ModelFilters["context"], number> = {
  any: 0,
  "32k": 32_000,
  "128k": 128_000,
  "1m": 1_000_000,
};

const SIZE_MAX: Record<ModelFilters["size"], number> = {
  any: Infinity,
  lte8: 8,
  lte16: 16,
  lte32: 32,
};

export function matchesFilters(m: CatalogModel, f: ModelFilters): boolean {
  if (f.price === "free" && !isFree(m)) return false;
  if (f.price === "lte1" || f.price === "lte5") {
    const limit = f.price === "lte1" ? 1 : 5;
    // Цена в каталоге хранится за 1000 токенов, показываем и фильтруем за 1M.
    const perM =
      m.cost_per_1k_input == null ? null : m.cost_per_1k_input * 1000;
    if (perM == null || perM > limit) return false;
  }
  if (f.size !== "any") {
    const vram = m.vram_gb_estimate;
    // Неизвестный вес не выдаём за подходящий: обещание «влезет» дороже
    // пропущенной строки.
    if (vram == null || vram > SIZE_MAX[f.size]) return false;
  }
  if (f.context !== "any") {
    if (
      !m.max_context_tokens ||
      m.max_context_tokens < CONTEXT_MIN[f.context]
    ) {
      return false;
    }
  }
  if (f.tools && !m.supports_tool_calling) return false;
  if (f.vision && !m.modalities.includes("vision")) return false;
  if (f.thinking && !m.thinking_supported) return false;
  return true;
}

const selectCls =
  "rounded border border-slate-600 bg-slate-800 px-1.5 py-1 text-[11px] " +
  "text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500";

function Toggle({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: string;
  title: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      title={title}
      className={`rounded border px-2 py-1 text-[11px] transition-colors ${
        active
          ? "border-blue-500 bg-blue-600/20 text-blue-300"
          : "border-slate-600 bg-slate-800 text-slate-300 hover:text-slate-100"
      }`}
    >
      {children}
    </button>
  );
}

export function ModelFilterBar({
  value,
  onChange,
  /** Для локального провайдера спрашивать про цену бессмысленно, и наоборот. */
  local,
}: {
  value: ModelFilters;
  onChange: (next: ModelFilters) => void;
  local: boolean;
}) {
  const set = (patch: Partial<ModelFilters>) =>
    onChange({ ...value, ...patch });

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {local ? (
        <label className="flex items-center gap-1 text-[11px] text-slate-400">
          Размер
          <select
            className={selectCls}
            value={value.size}
            onChange={(e) =>
              set({ size: e.target.value as ModelFilters["size"] })
            }
          >
            <option value="any">любой</option>
            <option value="lte8">до 8 GB</option>
            <option value="lte16">до 16 GB</option>
            <option value="lte32">до 32 GB</option>
          </select>
        </label>
      ) : (
        <label className="flex items-center gap-1 text-[11px] text-slate-400">
          Цена
          <select
            className={selectCls}
            value={value.price}
            onChange={(e) =>
              set({ price: e.target.value as ModelFilters["price"] })
            }
          >
            <option value="any">любая</option>
            <option value="free">бесплатные</option>
            <option value="lte1">до $1 / 1M</option>
            <option value="lte5">до $5 / 1M</option>
          </select>
        </label>
      )}

      <label className="flex items-center gap-1 text-[11px] text-slate-400">
        Контекст
        <select
          className={selectCls}
          value={value.context}
          onChange={(e) =>
            set({ context: e.target.value as ModelFilters["context"] })
          }
        >
          <option value="any">любой</option>
          <option value="32k">от 32K</option>
          <option value="128k">от 128K</option>
          <option value="1m">от 1M</option>
        </select>
      </label>

      <Toggle
        active={value.tools}
        onClick={() => set({ tools: !value.tools })}
        title="Только модели, умеющие вызывать инструменты"
      >
        инструменты
      </Toggle>
      <Toggle
        active={value.vision}
        onClick={() => set({ vision: !value.vision })}
        title="Только модели, читающие изображения"
      >
        зрение
      </Toggle>
      <Toggle
        active={value.thinking}
        onClick={() => set({ thinking: !value.thinking })}
        title="Только модели, умеющие рассуждать перед ответом"
      >
        рассуждение
      </Toggle>

      {filtersActive(value) && (
        <button
          type="button"
          onClick={() => onChange(EMPTY_FILTERS)}
          className="px-1.5 py-1 text-[11px] text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
        >
          сбросить
        </button>
      )}
    </div>
  );
}
