"use client";

// Action cells (link / download / delete) — lifted from canvas-table so the
// shared DataGrid can render the same actionable columns spec-tables emit.

import { useState } from "react";
import type { GridColumnType } from "./types";

export function getAction(value: unknown): {
  href?: string;
  label?: string;
  confirm?: string;
  method?: string;
} {
  if (typeof value === "string") return { href: value };
  if (!value || typeof value !== "object") return {};
  const record = value as Record<string, unknown>;
  return {
    href: typeof record.href === "string" ? record.href : undefined,
    label: typeof record.label === "string" ? record.label : undefined,
    confirm: typeof record.confirm === "string" ? record.confirm : undefined,
    method: typeof record.method === "string" ? record.method : undefined,
  };
}

export function ActionCell({
  value,
  type,
}: {
  value: unknown;
  type: GridColumnType | undefined;
}) {
  const [status, setStatus] = useState<"idle" | "pending" | "done" | "error">(
    "idle",
  );
  const action = getAction(value);
  if (!action.href) return <span className="text-slate-500">—</span>;

  if (type === "delete") {
    async function runDelete() {
      if (action.confirm && !window.confirm(action.confirm)) return;
      setStatus("pending");
      try {
        const res = await fetch(action.href!, {
          method: action.method || "DELETE",
        });
        setStatus(res.ok ? "done" : "error");
      } catch {
        setStatus("error");
      }
    }

    return (
      <button
        onClick={runDelete}
        disabled={status === "pending" || status === "done"}
        className="text-red-300 underline hover:text-red-200 disabled:text-slate-500"
      >
        {status === "pending"
          ? "Удаляю..."
          : status === "done"
            ? "Удалено"
            : status === "error"
              ? "Ошибка"
              : action.label || "Удалить"}
      </button>
    );
  }

  return (
    <a
      href={action.href}
      download={type === "download" ? true : undefined}
      target={type === "link" ? "_blank" : undefined}
      rel={type === "link" ? "noopener" : undefined}
      className="text-blue-300 underline hover:text-blue-200"
    >
      {action.label || (type === "download" ? "Скачать" : action.href)}
    </a>
  );
}
