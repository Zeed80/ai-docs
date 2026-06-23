"use client";

// Generic inline-editable grid cell — generalisation of EditableNotesCell and
// the review line-items EditableCell. Click to edit, Enter/blur commit, Esc
// cancel. Persistence is the caller's job: `onCommit` receives the new value
// and may be async (throwing → cell shows an error border, stays in edit mode).

import { useEffect, useRef, useState } from "react";
import type { GridColumnType } from "./types";

interface EditableCellProps {
  value: unknown;
  /** Seed value shown while editing (e.g. the raw formula vs the computed value). */
  editValue?: unknown;
  type?: GridColumnType;
  options?: string[];
  /** Render-only display (formatting) for the non-editing state. */
  display?: (value: unknown) => React.ReactNode;
  onCommit: (next: string) => void | Promise<void>;
}

function asText(value: unknown): string {
  if (value == null) return "";
  return String(value);
}

export function EditableCell({
  value,
  editValue,
  type = "text",
  options,
  display,
  onCommit,
}: EditableCellProps) {
  const seed = editValue !== undefined ? editValue : value;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(asText(seed));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const inputRef = useRef<HTMLInputElement | HTMLSelectElement>(null);

  useEffect(() => {
    if (!editing) setDraft(asText(seed));
  }, [seed, editing]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      if (inputRef.current instanceof HTMLInputElement) {
        inputRef.current.select();
      }
    }
  }, [editing]);

  const commit = async () => {
    if (draft === asText(seed)) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError(false);
    try {
      await onCommit(draft);
      setEditing(false);
    } catch {
      setError(true);
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(asText(seed));
    setError(false);
    setEditing(false);
  };

  if (editing) {
    const cls = `w-full min-w-[80px] rounded border bg-slate-900 px-1.5 py-0.5 text-xs text-slate-100 outline-none ${
      error ? "border-red-500" : "border-blue-500"
    }`;
    if (type === "select" && options) {
      return (
        <select
          ref={inputRef as React.RefObject<HTMLSelectElement>}
          value={draft}
          disabled={saving}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Escape") cancel();
          }}
          className={cls}
        >
          {!options.includes(draft) && (
            <option value={draft}>{draft || "—"}</option>
          )}
          {options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      );
    }
    return (
      <input
        ref={inputRef as React.RefObject<HTMLInputElement>}
        type={type === "number" ? "text" : type === "date" ? "date" : "text"}
        inputMode={type === "number" ? "decimal" : undefined}
        value={draft}
        disabled={saving}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") {
            e.preventDefault();
            void commit();
          } else if (e.key === "Escape") {
            cancel();
          }
        }}
        className={cls}
      />
    );
  }

  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        setEditing(true);
      }}
      title="Нажмите, чтобы редактировать"
      className="min-h-[1.4rem] cursor-text whitespace-pre-line rounded px-1 py-0.5 hover:bg-slate-700/40"
    >
      {value == null || value === "" ? (
        <span className="text-slate-600 italic">—</span>
      ) : display ? (
        display(value)
      ) : (
        asText(value)
      )}
    </div>
  );
}
