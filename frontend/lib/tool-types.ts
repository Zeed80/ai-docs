/**
 * Tool type labels — one map for the whole product.
 *
 * There were three copies with different coverage (9, 14 and 15 entries), so a
 * position of a type missing from one of them showed up as raw English in that
 * view and translated in another.
 */
export const TOOL_TYPE_LABELS: Record<string, string> = {
  drill: "Сверло",
  endmill: "Концевая фреза",
  milling_cutter: "Фреза",
  insert: "Пластина",
  holder: "Держатель",
  tap: "Метчик",
  reamer: "Развёртка",
  boring_bar: "Расточная оправка",
  thread_mill: "Резьбофреза",
  grinder: "Шлифкруг",
  turning_tool: "Резец",
  countersink: "Зенковка",
  counterbore: "Цековка",
  saw: "Дисковая пила",
  other: "Прочее",
};

export function toolTypeLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return TOOL_TYPE_LABELS[value] ?? value;
}
