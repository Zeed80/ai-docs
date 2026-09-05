"use client";

import { useMemo, useState } from "react";

import type {
  KernelEdgeDescriptor,
  KernelFaceDescriptor,
} from "@/lib/studio-api";

/** Searchable kernel-truth topology list. Small entities are deliberately
 * sorted first: they are the hardest to hit with a pointer in a dense model,
 * while their stable content keys remain exact and safe to select here. */
export default function TopologyPickerPanel({
  faces,
  edges,
  selectedFaceKey,
  selectedEdgeKey,
  onSelectFace,
  onSelectEdge,
}: {
  faces: KernelFaceDescriptor[];
  edges: KernelEdgeDescriptor[];
  selectedFaceKey: string | null;
  selectedEdgeKey: string | null;
  onSelectFace: (key: string | null) => void;
  onSelectEdge: (key: string | null) => void;
}) {
  const [kind, setKind] = useState<"face" | "edge">("face");
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const visibleFaces = useMemo(
    () =>
      [...faces]
        .filter(
          (face) =>
            !normalizedQuery ||
            face.key.toLowerCase().includes(normalizedQuery) ||
            face.surface.toLowerCase().includes(normalizedQuery) ||
            String(face.index) === normalizedQuery,
        )
        .sort((a, b) => a.area_mm2 - b.area_mm2 || a.index - b.index),
    [faces, normalizedQuery],
  );
  const visibleEdges = useMemo(
    () =>
      [...edges]
        .filter(
          (edge) =>
            !normalizedQuery ||
            edge.key.toLowerCase().includes(normalizedQuery) ||
            edge.curve.toLowerCase().includes(normalizedQuery) ||
            String(edge.index) === normalizedQuery,
        )
        .sort((a, b) => a.length_mm - b.length_mm || a.index - b.index),
    [edges, normalizedQuery],
  );
  const items = kind === "face" ? visibleFaces : visibleEdges;

  return (
    <section className="border-t border-white/10" data-testid="topology-picker">
      <div className="flex items-center justify-between gap-2 px-3 py-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
          Топология
        </span>
        <span className="text-[9px] text-zinc-400">мелкие сначала</span>
      </div>
      <div className="flex gap-1 px-2 pb-2">
        <button
          type="button"
          aria-pressed={kind === "face"}
          onClick={() => setKind("face")}
          className={`rounded px-2 py-1 text-[10px] ${
            kind === "face"
              ? "bg-sky-500/20 text-sky-200"
              : "bg-white/5 text-zinc-400"
          }`}
        >
          Грани {faces.length}
        </button>
        <button
          type="button"
          aria-pressed={kind === "edge"}
          onClick={() => setKind("edge")}
          className={`rounded px-2 py-1 text-[10px] ${
            kind === "edge"
              ? "bg-sky-500/20 text-sky-200"
              : "bg-white/5 text-zinc-400"
          }`}
        >
          Рёбра {edges.length}
        </button>
      </div>
      <div className="px-2 pb-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Поиск topology ID"
          placeholder="ID, индекс или тип"
          className="w-full rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-200 outline-none placeholder:text-zinc-400 focus:border-sky-500/50"
        />
      </div>
      {items.length === 0 ? (
        <p className="px-3 pb-3 text-[10px] text-zinc-400">
          {faces.length === 0 && edges.length === 0
            ? "Kernel topology descriptors недоступны."
            : "Совпадений нет."}
        </p>
      ) : (
        <ul className="max-h-48 overflow-y-auto border-t border-white/5">
          {kind === "face"
            ? visibleFaces.map((face) => {
                const active = face.key === selectedFaceKey;
                return (
                  <li key={face.key}>
                    <button
                      type="button"
                      aria-pressed={active}
                      title={face.key}
                      onClick={() => {
                        onSelectEdge(null);
                        onSelectFace(active ? null : face.key);
                      }}
                      className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[10px] ${
                        active
                          ? "bg-sky-500/15 text-sky-100"
                          : "text-zinc-400 hover:bg-white/5"
                      }`}
                    >
                      <span className="w-12 shrink-0 font-mono">
                        F{face.index}
                      </span>
                      <span className="min-w-0 flex-1 truncate">
                        {face.surface}
                      </span>
                      <span className="shrink-0 font-mono text-zinc-400">
                        {face.area_mm2.toPrecision(4)} mm²
                      </span>
                    </button>
                  </li>
                );
              })
            : visibleEdges.map((edge) => {
                const active = edge.key === selectedEdgeKey;
                return (
                  <li key={edge.key}>
                    <button
                      type="button"
                      aria-pressed={active}
                      title={edge.key}
                      onClick={() => {
                        onSelectFace(null);
                        onSelectEdge(active ? null : edge.key);
                      }}
                      className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[10px] ${
                        active
                          ? "bg-sky-500/15 text-sky-100"
                          : "text-zinc-400 hover:bg-white/5"
                      }`}
                    >
                      <span className="w-12 shrink-0 font-mono">
                        E{edge.index}
                      </span>
                      <span className="min-w-0 flex-1 truncate">
                        {edge.curve}
                      </span>
                      <span className="shrink-0 font-mono text-zinc-400">
                        {edge.length_mm.toPrecision(4)} mm
                      </span>
                    </button>
                  </li>
                );
              })}
        </ul>
      )}
    </section>
  );
}
