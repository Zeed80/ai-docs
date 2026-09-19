"use client";

/** Итог чтения строительного листа или схемы (план, X5/Ф7).
 *
 * У этих типов результат — не тело, а модель здания или системы. Раньше
 * /cad им отказывал; теперь оператор видит, что построено и что исключено:
 * стена без известной высоты или непрямая — не угадывается, а перечисляется.
 */

export type DomainReading = {
  domain: "construction" | "system";
  profile?: string | null;
  summary: string;
  report: {
    skipped?: { id?: string; kind?: string; reason?: string }[];
    blocked?: boolean;
    blocked_reason?: string;
  };
  /** Отметки уровня по знакам на листе (E10): число прочитано только у
   *  найденного знака или рамки отметки. */
  levels?: {
    values?: string[];
    places_found?: number;
    rejected?: number;
    error?: string;
  } | null;
};

export default function DomainReadingPanel({
  reading,
  t,
}: {
  reading?: DomainReading;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  if (!reading) return null;
  const skipped = reading.report?.skipped ?? [];
  return (
    <section
      className="rounded border border-white/10 bg-zinc-950/60 p-3 text-xs"
      aria-labelledby="domain-reading-title"
    >
      <h3
        id="domain-reading-title"
        className="mb-1 text-sm font-medium text-zinc-200"
      >
        {t(
          reading.domain === "construction"
            ? "vector.domain_title_construction"
            : "vector.domain_title_system",
        )}
      </h3>
      <p className="text-zinc-300">{reading.summary}</p>
      {reading.levels?.values?.length ? (
        <p className="mt-1 text-zinc-400">
          {t("vector.domain_levels", {
            list: reading.levels.values.join(" · "),
            found: reading.levels.places_found ?? reading.levels.values.length,
          })}
        </p>
      ) : null}
      {skipped.length ? (
        <details className="mt-2 text-zinc-400">
          <summary>{t("vector.domain_skipped", { count: skipped.length })}</summary>
          <ul className="mt-1 space-y-0.5">
            {skipped.slice(0, 50).map((item, index) => (
              <li key={`${item.id ?? index}`} className="break-all">
                {[item.kind, item.id, item.reason].filter(Boolean).join(" · ")}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
