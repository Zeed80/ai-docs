"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
import { useEffect, useRef, useState } from "react";

const API = getApiBaseUrl();

interface EditableNotesCellProps {
  invoiceId: string;
  value: string | null;
  onSaved: (next: string) => void;
}

// Inline-editable invoice note. Click to edit, Enter/blur to save (Shift+Enter
// for a newline), Escape to cancel. Persists via PATCH /api/invoices/{id}.
export function EditableNotesCell({
  invoiceId,
  value,
  onSaved,
}: EditableNotesCellProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!editing) setDraft(value ?? "");
  }, [value, editing]);

  useEffect(() => {
    if (editing && ref.current) {
      ref.current.focus();
      ref.current.selectionStart = ref.current.value.length;
    }
  }, [editing]);

  const save = async () => {
    const next = draft.trim();
    if (next === (value ?? "").trim()) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError(false);
    try {
      const res = await mutFetch(`${API}/api/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: next }),
      });
      if (!res.ok) throw new Error("save failed");
      onSaved(next);
      setEditing(false);
    } catch {
      setError(true);
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <textarea
        ref={ref}
        value={draft}
        disabled={saving}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void save();
          } else if (e.key === "Escape") {
            setDraft(value ?? "");
            setEditing(false);
          }
        }}
        rows={Math.min(Math.max(draft.split("\n").length, 2), 6)}
        className={`w-full min-w-[140px] resize-y rounded border bg-slate-900 px-2 py-1 text-xs text-slate-100 outline-none ${
          error ? "border-red-500" : "border-blue-500"
        }`}
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
      className="min-h-[1.5rem] cursor-text whitespace-pre-line rounded px-1 py-0.5 text-slate-300 hover:bg-slate-700/40"
    >
      {value ? value : <span className="text-slate-600 italic">добавить…</span>}
    </div>
  );
}
