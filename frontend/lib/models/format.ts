/**
 * Форматирование чисел раздела «Модели».
 *
 * Контекстное окно и цена нигде не показывались, хотя max_context_tokens и
 * cost_per_1k_* лежат в схеме модели с самого начала.
 */

/** 131072 → «128K», 1048576 → «1M». */
export function formatContext(tokens: number | null | undefined): string {
  if (!tokens || tokens <= 0) return "—";
  if (tokens >= 1_000_000) {
    const m = tokens / 1_048_576;
    return `${m >= 10 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (tokens >= 1000) return `${Math.round(tokens / 1024)}K`;
  return String(tokens);
}

/** 9.6 → «9.6 GB». */
export function formatVram(gb: number | null | undefined): string {
  if (!gb || gb <= 0) return "—";
  return `${gb >= 10 ? Math.round(gb) : gb.toFixed(1).replace(/\.0$/, "")} GB`;
}

/**
 * Цена за миллион токенов, как её публикуют провайдеры.
 * В каталоге хранится за 1000 — умножаем, чтобы не показывать «$0.003».
 */
export function formatPricePerMTok(
  costPer1k: number | null | undefined,
): string | null {
  if (costPer1k == null || costPer1k <= 0) return null;
  const perM = costPer1k * 1000;
  return perM >= 1 ? `$${perM.toFixed(2)}` : `$${perM.toFixed(3)}`;
}

/** Пара ввод/вывод: «$3.00/$15.00 за 1M». */
export function formatPricePair(
  input: number | null | undefined,
  output: number | null | undefined,
): string | null {
  const i = formatPricePerMTok(input);
  const o = formatPricePerMTok(output);
  if (!i && !o) return null;
  if (i && o) return `${i}/${o} за 1M`;
  return `${i ?? o} за 1M`;
}

/** 1847 → «1.8 с», 640 → «640 мс». */
export function formatLatency(ms: number | null | undefined): string {
  if (ms == null || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} мс`;
  return `${(ms / 1000).toFixed(1).replace(/\.0$/, "")} с`;
}

/** 0.005 → «0.5%». Ноль показываем как «0%», а не как прочерк. */
export function formatErrorRate(rate: number | null | undefined): string {
  if (rate == null) return "—";
  const pct = rate * 100;
  if (pct === 0) return "0%";
  return `${pct < 1 ? pct.toFixed(1) : Math.round(pct)}%`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}
