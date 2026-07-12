"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";

import CadWorkspace from "@/components/cad/CadWorkspace";
import { Generation, getGeneration } from "@/lib/studio-api";

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
        <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {gen.error || t("status_failed")}
        </p>
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
