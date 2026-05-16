"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface User {
  sub: string;
  name: string;
  email: string;
  role: string;
}

interface Props {
  entityType: string;
  entityId: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function ForwardModal({
  entityType,
  entityId,
  onClose,
  onSuccess,
}: Props) {
  const [users, setUsers] = useState<User[]>([]);
  const [toUser, setToUser] = useState("");
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/auth/users`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then((data: User[]) => {
        setUsers(data);
        if (data.length > 0) setToUser(data[0].sub);
      })
      .catch(() => setUsers([]));
  }, []);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!toUser || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/handovers`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...csrfHeaders(),
        },
        body: JSON.stringify({
          entity_type: entityType,
          entity_id: entityId,
          to_user: toUser,
          comment: comment.trim() || undefined,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body?.detail ?? "Ошибка при пересылке");
        return;
      }
      setSuccess(true);
      setTimeout(() => {
        onSuccess();
        onClose();
      }, 800);
    } catch {
      setError("Ошибка соединения");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-full max-w-md mx-4 rounded-lg bg-slate-800 border border-slate-700 p-6 shadow-xl">
        <h3 className="text-sm font-semibold text-slate-100 mb-4">
          Переслать коллеге
        </h3>
        {success ? (
          <p className="text-sm text-green-400 py-4 text-center">
            Документ успешно переслан
          </p>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Получатель
              </label>
              <select
                value={toUser}
                onChange={(e) => setToUser(e.target.value)}
                required
                className="w-full rounded border border-slate-600 bg-slate-700 text-slate-100 text-sm px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {users.map((u) => (
                  <option key={u.sub} value={u.sub}>
                    {u.name} ({u.email})
                  </option>
                ))}
                {users.length === 0 && (
                  <option value="" disabled>
                    Загрузка...
                  </option>
                )}
              </select>
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Комментарий (необязательно)
              </label>
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="Добавьте комментарий..."
                rows={3}
                className="w-full resize-none rounded border border-slate-600 bg-slate-700 text-slate-100 text-sm px-2 py-1.5 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            {error && <p className="text-xs text-red-400">{error}</p>}
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={onClose}
                className="rounded border border-slate-600 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700"
              >
                Отмена
              </button>
              <button
                type="submit"
                disabled={submitting || !toUser}
                className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {submitting ? "Отправка..." : "Переслать"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
