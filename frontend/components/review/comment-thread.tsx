"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useEffect, useRef, useState } from "react";

const API = getApiBaseUrl();

interface CommentOut {
  id: string;
  entity_type: string;
  entity_id: string;
  author_sub: string;
  author_name: string;
  text: string;
  parent_id: string | null;
  created_at: string;
}

interface Props {
  entityType: string;
  entityId: string;
}

export function CommentThread({ entityType, entityId }: Props) {
  const [comments, setComments] = useState<CommentOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState("");
  const [parentId, setParentId] = useState<string | null>(null);
  const [parentText, setParentText] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  async function load() {
    try {
      const res = await fetch(
        `${API}/api/comments?entity_type=${encodeURIComponent(entityType)}&entity_id=${encodeURIComponent(entityId)}`,
        { credentials: "include" },
      );
      if (!res.ok) return;
      setComments(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [entityType, entityId]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${API}/api/comments`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          entity_type: entityType,
          entity_id: entityId,
          text: trimmed,
          parent_id: parentId ?? undefined,
        }),
      });
      if (!res.ok) return;
      setText("");
      setParentId(null);
      setParentText("");
      await load();
    } finally {
      setSubmitting(false);
    }
  }

  function handleReply(c: CommentOut) {
    setParentId(c.id);
    setParentText(c.text.length > 60 ? c.text.slice(0, 60) + "…" : c.text);
    textareaRef.current?.focus();
  }

  function cancelReply() {
    setParentId(null);
    setParentText("");
  }

  const topLevel = comments.filter((c) => c.parent_id === null);
  const replies = (parentCommentId: string) =>
    comments.filter((c) => c.parent_id === parentCommentId);

  function renderComment(c: CommentOut, isReply = false) {
    return (
      <div
        key={c.id}
        className={`${isReply ? "ml-6 border-l-2 border-slate-100 pl-3" : ""}`}
      >
        <div className="rounded-lg bg-slate-50 p-3 text-sm">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="font-medium text-slate-700 text-xs">
              {c.author_name}
            </span>
            <span className="text-xs text-slate-400">
              {new Date(c.created_at).toLocaleString("ru-RU")}
            </span>
          </div>
          <p className="text-slate-800 whitespace-pre-wrap break-words">
            {c.text}
          </p>
          {!isReply && (
            <button
              type="button"
              onClick={() => handleReply(c)}
              className="mt-1.5 text-xs text-blue-500 hover:text-blue-700"
            >
              Ответить
            </button>
          )}
        </div>
        {replies(c.id).map((r) => renderComment(r, true))}
      </div>
    );
  }

  return (
    <div className="bg-white border border-slate-200 rounded-lg p-4">
      <h3 className="text-sm font-semibold mb-3">Комментарии</h3>

      {loading ? (
        <p className="text-xs text-slate-400">Загрузка...</p>
      ) : comments.length === 0 ? (
        <p className="text-xs text-slate-400 mb-3">Пока нет комментариев</p>
      ) : (
        <div className="space-y-3 mb-4">
          {topLevel.map((c) => renderComment(c))}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-2">
        {parentId && (
          <div className="flex items-start justify-between rounded bg-blue-50 px-3 py-2 text-xs text-blue-700">
            <span className="line-clamp-1">
              <span className="font-medium">Ответ на: </span>
              {parentText}
            </span>
            <button
              type="button"
              onClick={cancelReply}
              className="ml-2 shrink-0 text-blue-400 hover:text-blue-600"
            >
              ✕
            </button>
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              handleSubmit(e as unknown as React.FormEvent);
            }
          }}
          placeholder="Написать комментарий… (Ctrl+Enter для отправки)"
          rows={3}
          className="w-full resize-none rounded border border-slate-200 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
        />
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={submitting || !text.trim()}
            className="rounded bg-blue-500 px-3 py-1.5 text-sm text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {submitting ? "Отправка…" : "Отправить"}
          </button>
        </div>
      </form>
    </div>
  );
}
