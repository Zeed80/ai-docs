"use client";

import type { ReactNode } from "react";
import { card, cardHeader } from "./tokens";

export function SectionCard({
  title,
  actions,
  children,
  className = "",
}: {
  title: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`${card} ${className}`}>
      <header className={cardHeader}>
        <h3 className="text-sm font-medium text-slate-200">{title}</h3>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}
