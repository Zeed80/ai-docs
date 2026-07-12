"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  createBlankSheet,
  Generation,
  importDxf,
  listGenerations,
  resultUrl,
} from "@/lib/studio-api";

type SheetFormat = "A4" | "A3" | "A2" | "A1";

function docTitle(g: Generation): string {
  const params = (g.params ?? {}) as Record<string, unknown>;
  return (
    g.prompt ||
    (params.source_filename as string | undefined) ||
    (params.sheet_format ? `Лист ${params.sheet_format}` : "") ||
    g.id.slice(0, 8)
  );
}

function docKind(g: Generation): "blank" | "imported" | "scan" {
  const params = (g.params ?? {}) as Record<string, unknown>;
  if (params.blank) return "blank";
  if (params.imported) return "imported";
  return "scan";
}

export default function CadListPage() {
  const t = useTranslations("cad");
  const router = useRouter();
  const [items, setItems] = useState<Generation[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showNewSheet, setShowNewSheet] = useState(false);
  const [sheetFormat, setSheetFormat] = useState<SheetFormat>("A4");
  const [landscape, setLandscape] = useState(false);
  const [withFrame, setWithFrame] = useState(true);
  const [sheetTitle, setSheetTitle] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await listGenerations();
      setItems(data.filter((g) => g.operation === "vectorize"));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((g) => docTitle(g).toLowerCase().includes(q));
  }, [items, query]);

  const onCreateSheet = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const gen = await createBlankSheet({
        format: sheetFormat,
        landscape,
        with_frame: withFrame,
        title: sheetTitle.trim() || undefined,
      });
      router.push(`/cad/${gen.id}`);
    } catch (e) {
      setError(String((e as Error).message || e));
      setBusy(false);
    }
  }, [landscape, router, sheetFormat, sheetTitle, withFrame]);

  const onImport = useCallback(
    async (file: File) => {
      setBusy(true);
      setError(null);
      try {
        const gen = await importDxf(file);
        router.push(`/cad/${gen.id}`);
      } catch (e) {
        setError(String((e as Error).message || e));
        setBusy(false);
      }
    },
    [router],
  );

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 md:px-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">{t("title")}</h1>
          <p className="text-sm text-zinc-400">{t("subtitle")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/studio"
            className="rounded border border-white/15 px-3 py-2 text-sm text-zinc-300 hover:bg-white/5"
          >
            {t("open_studio")}
          </Link>
          <button
            type="button"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
            className="rounded border border-white/15 px-3 py-2 text-sm text-zinc-200 hover:bg-white/5 disabled:opacity-50"
          >
            {t("import_dxf")}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".dxf"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void onImport(file);
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => setShowNewSheet((v) => !v)}
            className="rounded bg-sky-600 px-3 py-2 text-sm text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("new_sheet")}
          </button>
        </div>
      </header>

      {showNewSheet && (
        <section className="flex flex-wrap items-end gap-3 rounded-lg border border-white/10 bg-zinc-900/40 p-4">
          <label className="flex flex-col gap-1 text-xs text-zinc-400">
            {t("sheet_title")}
            <input
              value={sheetTitle}
              onChange={(e) => setSheetTitle(e.target.value)}
              className="rounded border border-white/15 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
              placeholder={t("sheet_title_placeholder")}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-400">
            {t("sheet_format")}
            <select
              value={sheetFormat}
              onChange={(e) => setSheetFormat(e.target.value as SheetFormat)}
              className="rounded border border-white/15 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
            >
              {(["A4", "A3", "A2", "A1"] as const).map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={landscape}
              onChange={(e) => setLandscape(e.target.checked)}
            />
            {t("landscape")}
          </label>
          <label className="flex items-center gap-2 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={withFrame}
              onChange={(e) => setWithFrame(e.target.checked)}
            />
            {t("with_frame")}
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => void onCreateSheet()}
            className="rounded bg-sky-600 px-3 py-1.5 text-sm text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("create")}
          </button>
        </section>
      )}

      {error && (
        <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("search")}
        className="w-full max-w-md rounded border border-white/15 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
      />

      {filtered.length === 0 ? (
        <p className="py-12 text-center text-sm text-zinc-500">{t("empty")}</p>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {filtered.map((g) => {
            const kind = docKind(g);
            const processing = g.status === "queued" || g.status === "running";
            return (
              <Link
                key={g.id}
                href={`/cad/${g.id}`}
                className="group overflow-hidden rounded-lg border border-white/10 bg-zinc-900/40 transition hover:border-sky-500/50"
              >
                <div className="flex aspect-[4/3] items-center justify-center bg-zinc-950">
                  {g.has_result ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={resultUrl(g.id, true)}
                      alt={docTitle(g)}
                      className="h-full w-full object-contain"
                      loading="lazy"
                    />
                  ) : (
                    <span className="text-xs text-zinc-600">
                      {processing ? t("status_processing") : "DXF"}
                    </span>
                  )}
                </div>
                <div className="space-y-1 p-3">
                  <p
                    className="truncate text-sm text-zinc-200"
                    title={docTitle(g)}
                  >
                    {docTitle(g)}
                  </p>
                  <div className="flex flex-wrap items-center gap-1 text-[11px]">
                    <span className="rounded bg-white/5 px-1.5 py-0.5 text-zinc-400">
                      {t(`badge_${kind}`)}
                    </span>
                    {g.status === "failed" ? (
                      <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-red-300">
                        {t("status_failed")}
                      </span>
                    ) : processing ? (
                      <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">
                        {t("status_processing")}
                      </span>
                    ) : g.accepted ? (
                      <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">
                        {t("status_accepted")}
                      </span>
                    ) : (
                      <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-sky-300">
                        {t("status_draft")}
                      </span>
                    )}
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </main>
  );
}
