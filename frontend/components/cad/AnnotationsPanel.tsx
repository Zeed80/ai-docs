"use client";

import { useState } from "react";

import { TOLERANCE_GLYPHS } from "@/components/cad/geometry";

type Kind = "roughness" | "thread" | "tolerance" | "datum" | "weld";

const KINDS: { key: Kind; label: string }[] = [
  { key: "roughness", label: "Шероховатость" },
  { key: "thread", label: "Резьба" },
  { key: "tolerance", label: "Допуск" },
  { key: "datum", label: "База" },
  { key: "weld", label: "Сварка" },
];

/** C4: place a structured ЕСКД annotation. The parent computes the drop
 * position (view centre) and commits it via an `add` op. */
export default function AnnotationsPanel({
  busy,
  onAdd,
  t,
}: {
  busy: boolean;
  onAdd: (payload: {
    kind: Kind;
    value?: string;
    symbol?: string;
    datum_refs?: string[];
  }) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("roughness");
  const [value, setValue] = useState("");
  const [symbol, setSymbol] = useState("perpendicularity");
  const [datums, setDatums] = useState("");

  function add() {
    const payload: {
      kind: Kind;
      value?: string;
      symbol?: string;
      datum_refs?: string[];
    } = { kind };
    if (kind === "datum") {
      payload.symbol = value.trim() || "A";
    } else if (kind === "tolerance") {
      payload.symbol = symbol;
      payload.value = value.trim() || undefined;
      const refs = datums
        .split(/[ ,]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (refs.length) payload.datum_refs = refs;
    } else {
      payload.value = value.trim() || undefined;
    }
    onAdd(payload);
    setValue("");
    setDatums("");
  }

  return (
    <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-xs text-zinc-300"
      >
        <span>{t("vector.annotations")}</span>
        <span className="text-zinc-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="space-y-1.5">
          <div className="flex flex-wrap gap-1">
            {KINDS.map((k) => (
              <button
                key={k.key}
                type="button"
                onClick={() => setKind(k.key)}
                className={`rounded px-2 py-0.5 text-[11px] ${
                  kind === k.key
                    ? "bg-sky-600 text-white"
                    : "bg-white/5 text-zinc-300 hover:bg-white/10"
                }`}
              >
                {k.label}
              </button>
            ))}
          </div>
          {kind === "tolerance" && (
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              className="w-full rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
            >
              {Object.entries(TOLERANCE_GLYPHS).map(([k, glyph]) => (
                <option key={k} value={k}>
                  {glyph} {k}
                </option>
              ))}
            </select>
          )}
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={
              kind === "roughness"
                ? "3.2"
                : kind === "thread"
                  ? "M20×1.5"
                  : kind === "datum"
                    ? "A"
                    : kind === "tolerance"
                      ? "0.05"
                      : "С2"
            }
            className="w-full rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
          />
          {kind === "tolerance" && (
            <input
              value={datums}
              onChange={(e) => setDatums(e.target.value)}
              placeholder={t("vector.annotation_datums")}
              className="w-full rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
            />
          )}
          <button
            type="button"
            disabled={busy}
            onClick={add}
            className="w-full rounded bg-sky-600 px-3 py-1.5 text-xs text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("vector.annotation_add")}
          </button>
        </div>
      )}
    </div>
  );
}
