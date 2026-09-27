"use client";

import { useTranslations } from "next-intl";

import CadModelViewer from "@/components/studio/CadModelViewer";
import { solidPreviewUrl } from "@/lib/studio-api";

/** Итог метода «по видам листа» (CAD_DIGITIZATION_AUDIT.md, раздел 9).
 *
 * Метод строит тело как инженер: основа (вращение или выдавливание),
 * масштаб по надписям, элементы по видам. Оператору важно не только «тело
 * есть», но и чего в нём заведомо нет: надписи листа, не найденные на теле
 * (E1), перечисляются поимённо.
 */

export type ViewsReading = {
  method?: "views";
  ok?: boolean;
  reason?: string;
  profile?: {
    kind?: "extrude";
    main_view?: string | number;
    role?: string;
    thickness_mm?: number;
  };
  features?: unknown[];
  scales?: Record<string, number>;
  notes?: string[];
  coverage?: {
    explained?: string[];
    missing?: string[];
    share?: number | null;
  };
};

export default function ViewsReadingPanel({
  reading,
  t,
}: {
  reading?: ViewsReading;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  if (!reading || reading.method !== "views") return null;
  const coverage = reading.coverage ?? {};
  const found = coverage.explained?.length ?? 0;
  const missing = coverage.missing ?? [];
  return (
    <section
      className="rounded border border-white/10 bg-zinc-950/60 p-3 text-xs"
      aria-labelledby="views-reading-title"
    >
      <h3
        id="views-reading-title"
        className="mb-1 text-sm font-medium text-zinc-200"
      >
        {t("vector.views_title")}
      </h3>
      {reading.ok ? (
        <p className="text-zinc-300">
          {reading.profile?.kind === "extrude"
            ? t("vector.views_base_extrude", {
                thickness: reading.profile?.thickness_mm ?? "?",
              })
            : t("vector.views_base_revolve", {
                view: String(reading.profile?.main_view ?? "?"),
              })}
          {reading.features?.length
            ? ` · ${t("vector.views_features", { count: reading.features.length })}`
            : ""}
        </p>
      ) : (
        <p className="break-words text-red-300">
          {t("vector.views_refused", { reason: reading.reason ?? "" })}
        </p>
      )}
      {found + missing.length > 0 ? (
        <p
          className={`mt-1 ${missing.length ? "text-amber-300" : "text-emerald-300"}`}
        >
          {t("vector.views_coverage", { found, total: found + missing.length })}
        </p>
      ) : null}
      {missing.length ? (
        <p className="mt-1 break-words text-zinc-400">
          {t("vector.views_missing", { list: missing.join(" · ") })}
        </p>
      ) : null}
      {reading.notes?.length ? (
        <details className="mt-1 text-zinc-500">
          <summary className="cursor-pointer">
            {t("vector.views_notes")}
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {reading.notes.map((note, index) => (
              <li key={index} className="break-words">
                {note}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

/** Страница прогона метода `views`: векторного листа у него нет, есть тело
 *  и отчёт. Раньше страница открывала векторный редактор и показывала
 *  «загрузка» — результат метода оператор не видел вовсе. */
export function ViewsResultView({
  generationId,
  reading,
  solid,
}: {
  generationId: string;
  reading: ViewsReading;
  solid?: { built?: boolean; paths?: Record<string, string> };
}) {
  const t = useTranslations("studio");
  return (
    <div className="flex flex-col gap-3 pb-24 sm:pb-0">
      <ViewsReadingPanel reading={reading} t={t} />
      {solid?.built && solid.paths?.stl ? (
        <section className="rounded border border-white/10 bg-zinc-950/60 p-3">
          <div className="flex flex-wrap gap-2 text-xs">
            {solid.paths?.step ? (
              <a
                href={solidPreviewUrl(generationId, "step")}
                className="rounded bg-white/10 px-3 py-1.5 text-zinc-200 hover:bg-white/15"
              >
                STEP
              </a>
            ) : null}
          </div>
          <div className="mt-3 overflow-hidden rounded border border-white/10 bg-zinc-950">
            <CadModelViewer
              url={solidPreviewUrl(generationId)}
              loadingLabel={t("vector.cad_preview_loading")}
              errorLabel={t("vector.cad_preview_error")}
            />
          </div>
        </section>
      ) : null}
    </div>
  );
}
