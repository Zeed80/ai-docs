"use client";

import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "error" | "muted" | "info";

const TONE: Record<Tone, string> = {
  ok: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  warn: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  error: "bg-red-500/10 text-red-400 border-red-500/30",
  info: "bg-blue-500/10 text-blue-400 border-blue-500/30",
  muted: "bg-slate-700/40 text-slate-400 border-slate-600/50",
};

export function Badge({
  tone = "muted",
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-medium ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}
