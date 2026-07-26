"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";

import CadWorkspace from "@/components/cad/CadWorkspace";
import { getApiBaseUrl } from "@/lib/api-base";
import { Generation, getGeneration } from "@/lib/studio-api";


/** What the reader managed to get off the sheet, shown when the build is blocked.
 *
 * A fail-closed contract is right — a part built from a misread sheet is worse
 * than no part. But the refusal was ALL the user got: a wall of internal
 * wording ("цепочка не сходится с числом ступеней (8 диаметров, 6 осевых
 * размеров)") and nothing else, while the reading itself sat in the
 * generation's params, unseen. The stamp, the callouts and the requirements
 * are usually right even when the geometry is not, and seeing them is what
 * lets a person tell a bad read from a hard drawing. */
function ReadSoFar({ spec }: { spec: Record<string, unknown> }) {
  const title = (spec.title_block ?? {}) as Record<string, unknown>;
  const dimensions = (spec.dimensions ?? []) as { value?: string }[];
  const annotations = (spec.annotations ?? []) as { text?: string }[];
  const body = (spec.main_view ?? {}) as Record<string, unknown>;
  const outer = (body.outer ?? []) as { diameter_mm?: number; length_mm?: number }[];
  const bore = (body.bore ?? []) as { diameter_mm?: number; length_mm?: number }[];

  const field = (label: string, value: unknown) =>
    value ? (
      <div key={label}>
        <span className="text-zinc-500">{label}: </span>
        <span className="text-zinc-200">{String(value)}</span>
      </div>
    ) : null;

  const steps = (items: { diameter_mm?: number; length_mm?: number }[]) =>
    items
      .map((s) => `⌀${s.diameter_mm ?? "?"}×${s.length_mm ?? "?"}`)
      .join("  ");

  return (
    <section className="rounded border border-zinc-700 bg-zinc-900/60 px-3 py-2 text-xs">
      <h2 className="mb-2 text-sm text-zinc-300">Что удалось прочитать с листа</h2>
      <div className="grid gap-1 sm:grid-cols-2">
        {field("Деталь", spec.part ?? title.name)}
        {field("Материал", title.material)}
        {field("Масштаб", title.scale)}
        {field("Обозначение", title.designation)}
      </div>
      {outer.length > 0 && (
        <p className="mt-2 break-words">
          <span className="text-zinc-500">Наружный контур: </span>
          <span className="text-zinc-200">{steps(outer)}</span>
        </p>
      )}
      {bore.length > 0 && (
        <p className="mt-1 break-words">
          <span className="text-zinc-500">Расточка: </span>
          <span className="text-zinc-200">{steps(bore)}</span>
        </p>
      )}
      {dimensions.length > 0 && (
        <p className="mt-2 break-words">
          <span className="text-zinc-500">Размеры ({dimensions.length}): </span>
          <span className="text-zinc-200">
            {dimensions.map((d) => d.value).filter(Boolean).join(", ")}
          </span>
        </p>
      )}
      {annotations.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {annotations.map((a, index) => (
            <li key={index} className="text-zinc-300">
              — {a.text}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-zinc-500">
        Чертёж не построен намеренно: часть данных прочитана неуверенно, и
        деталь по ним была бы неверной. Прочитанное выше — то, на что можно
        опереться.
      </p>
    </section>
  );
}

const API = getApiBaseUrl();

export default function CadEditorPage() {
  const t = useTranslations("cad");
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const [gen, setGen] = useState<Generation | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      setGen(await getGeneration(id));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // A digitization still in flight: poll until it lands.
  const processing = gen?.status === "queued" || gen?.status === "running";
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [processing, load]);

  return (
    <main className="flex min-h-screen flex-col bg-zinc-950 px-4 py-3">
      <header className="mb-3 flex flex-wrap items-center gap-3 border-b border-white/10 pb-3">
        <Link
          href="/cad"
          className="rounded border border-white/15 px-2.5 py-1.5 text-sm text-zinc-300 hover:bg-white/5"
        >
          ← {t("back_to_list")}
        </Link>
        <h1 className="truncate text-lg font-medium text-zinc-100">
          {gen?.prompt ||
            ((gen?.params as Record<string, unknown>)?.source_filename as
              string | undefined) ||
            t("title")}
        </h1>
        {gen && (
          <span className="text-xs text-zinc-500">
            {gen.accepted ? t("status_accepted") : t("status_draft")}
          </span>
        )}
        <span className="flex-1" />
        {gen && (
          <Link
            href={`/studio?id=${gen.id}`}
            className="text-xs text-zinc-500 hover:text-zinc-300"
          >
            {t("open_in_studio")}
          </Link>
        )}
      </header>

      {error && (
        <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}
      {!gen && !error && (
        <p className="py-12 text-center text-sm text-zinc-500">
          {t("loading")}
        </p>
      )}
      {gen && gen.status === "failed" && (
        // min-h-screen on the page means a failed run left the whole viewport
        // below the message as bare dark background — the "black square" this
        // looked like from the user's seat. The space is the sheet's now: the
        // drawing that was uploaded, beside what was read off it, which is
        // exactly the comparison a person needs to judge the refusal.
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {gen.error || t("status_failed")}
          </p>
          <div className="flex min-h-0 flex-1 flex-col gap-2 lg:flex-row">
            <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded border border-zinc-800 bg-zinc-900/40">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={`${API}/api/image-gen/${gen.id}/source`}
                alt={gen.source_image_paths?.[0] ?? ""}
                className="max-h-full max-w-full object-contain"
              />
            </div>
            {(gen.params?.spec as Record<string, unknown> | undefined) && (
              <div className="min-h-0 space-y-2 overflow-auto lg:w-[46%]">
                <ReadSoFar spec={gen.params.spec as Record<string, unknown>} />
                {Boolean(gen.params?.sheet_without_geometry) && (
                  <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                    Лист сохранён без геометрии детали: рамка, штамп,
                    технические требования и прочитанные размеры. Откройте его
                    в редакторе и достройте контур — читать надписи заново не
                    придётся.
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      )}
      {gen && processing && (
        <p className="py-12 text-center text-sm text-amber-300">
          {t("status_processing")}
        </p>
      )}
      {gen && gen.status === "done" && gen.operation === "vectorize" && (
        <div className="min-h-0 flex-1">
          <CadWorkspace gen={gen} onChanged={load} />
        </div>
      )}
    </main>
  );
}
