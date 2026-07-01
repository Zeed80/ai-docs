"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import {
  Generation,
  acceptGeneration,
  deleteGeneration,
  iterateGeneration,
  resultUrl,
  sourceUrl,
} from "@/lib/studio-api";

interface Props {
  gen: Generation;
  onChanged: () => void;
  onClose: () => void;
}

export default function GenerationDetail({ gen, onChanged, onClose }: Props) {
  const t = useTranslations("studio");
  const [iterPrompt, setIterPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const hasSource = (gen.source_image_paths?.length ?? 0) > 0;

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-zinc-200">
          {gen.operation} · {t(`status.${gen.status}`)}
          {gen.accepted ? t("status.accepted_suffix") : ""}
        </h3>
        <button
          onClick={onClose}
          className="text-zinc-400 hover:text-white text-sm"
        >
          ✕
        </button>
      </div>

      {gen.status === "failed" && (
        <div className="text-xs text-red-400 bg-red-500/10 rounded p-2">
          {gen.error}
        </div>
      )}

      {(gen.source_document_id || gen.case_id) && (
        <div className="flex flex-wrap gap-2 text-[11px]">
          {gen.source_document_id && (
            <span className="px-2 py-0.5 rounded bg-white/5 text-zinc-400">
              {t("detail.doc_badge", { id: gen.source_document_id })}
            </span>
          )}
          {gen.case_id && (
            <span className="px-2 py-0.5 rounded bg-white/5 text-zinc-400">
              {t("detail.case_badge", { id: gen.case_id })}
            </span>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        {hasSource && (
          <figure>
            <figcaption className="text-[11px] text-zinc-500 mb-1">
              {t("detail.source_label")}
            </figcaption>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={sourceUrl(gen.id, 0)}
              alt={t("composer.source_alt")}
              className="w-full rounded border border-white/10 bg-zinc-900"
            />
          </figure>
        )}
        {gen.has_result && (
          <figure className={hasSource ? "" : "col-span-2"}>
            <figcaption className="text-[11px] text-zinc-500 mb-1">
              {t("detail.result_label")}
            </figcaption>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={resultUrl(gen.id)}
              alt={t("gallery.result_alt")}
              className="w-full rounded border border-white/10 bg-zinc-900"
            />
          </figure>
        )}
      </div>

      {gen.prompt && <p className="text-xs text-zinc-400">{gen.prompt}</p>}

      {err && <div className="text-xs text-red-400">{err}</div>}

      {gen.has_result && (
        <div className="flex flex-wrap gap-2">
          {!gen.accepted && (
            <button
              disabled={busy}
              onClick={() => run(() => acceptGeneration(gen.id))}
              className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50"
            >
              {t("detail.accept")}
            </button>
          )}
          <a
            href={resultUrl(gen.id)}
            download={`studio-${gen.id}.png`}
            className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
          >
            {t("detail.download")}
          </a>
          <button
            disabled={busy}
            onClick={() => run(() => deleteGeneration(gen.id))}
            className="px-3 py-1.5 rounded bg-red-500/15 hover:bg-red-500/25 text-red-300 text-sm disabled:opacity-50"
          >
            {t("detail.delete")}
          </button>
        </div>
      )}

      {gen.has_result && (
        <div className="border-t border-white/10 pt-3">
          <label className="text-[11px] text-zinc-500">
            {t("detail.iterate_label")}
          </label>
          <textarea
            value={iterPrompt}
            onChange={(e) => setIterPrompt(e.target.value)}
            placeholder={t("detail.iterate_placeholder")}
            className="w-full mt-1 rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
            rows={2}
          />
          <button
            disabled={busy || !iterPrompt.trim()}
            onClick={() =>
              run(async () => {
                await iterateGeneration(gen.id, {
                  operation: "edit",
                  prompt: iterPrompt,
                });
                setIterPrompt("");
              })
            }
            className="mt-2 px-3 py-1.5 rounded bg-sky-600 hover:bg-sky-500 text-white text-sm disabled:opacity-50"
          >
            {t("detail.iterate_submit")}
          </button>
        </div>
      )}
    </div>
  );
}
