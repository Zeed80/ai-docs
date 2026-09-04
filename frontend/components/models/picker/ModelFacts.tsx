"use client";

import { Badge } from "@/components/ui/primitives/Badge";
import {
  formatContext,
  formatPricePair,
  formatVram,
} from "@/lib/models/format";
import { modalityLabel, providerLabel } from "@/lib/models/labels";
import type { CatalogModel } from "@/lib/models/types";

/**
 * Строка фактов о модели: контекст, что умеет, вес или цена.
 *
 * Раньше в списке выбора было только имя и «· N GB» — ни размера контекста, ни
 * умеет ли модель вызывать инструменты, ни сколько стоит облачная. Всё это
 * лежало в каталоге и не показывалось.
 */
export function ModelFacts({ model }: { model: CatalogModel }) {
  const price = formatPricePair(
    model.cost_per_1k_input,
    model.cost_per_1k_output,
  );
  const isLocal = model.local_only;

  const parts: string[] = [];
  if (model.max_context_tokens) {
    parts.push(`${formatContext(model.max_context_tokens)} контекст`);
  }
  const skills = model.modalities
    .filter((m) => m !== "text")
    .map(modalityLabel);
  if (model.supports_tool_calling) skills.push("инструменты");
  if (skills.length) parts.push(skills.join(", "));
  if (isLocal && model.vram_gb_estimate) {
    parts.push(formatVram(model.vram_gb_estimate));
  }
  if (price) parts.push(price);

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
      <span className="text-slate-400">{providerLabel(model.provider)}</span>
      {model.preferred_instance && (
        <span className="text-slate-500">· {model.preferred_instance}</span>
      )}
      {parts.length > 0 && <span>· {parts.join(" · ")}</span>}
      {model.thinking_supported && (
        <Badge tone="info" title="Модель умеет рассуждать перед ответом">
          рассуждение
        </Badge>
      )}
      {!isLocal && (
        <Badge tone="warn" title="Данные уйдут внешнему провайдеру">
          облако
        </Badge>
      )}
    </div>
  );
}
