"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();

interface Room {
  id: string;
  name: string;
  type: string;
  unread_count: number;
}

interface Props {
  entityType: string;
  entityId: string;
  entityTitle: string;
  onClose: () => void;
}

export function ShareToChatModal({
  entityType,
  entityId,
  entityTitle,
  onClose,
}: Props) {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [roomId, setRoomId] = useState("");
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/rooms`, { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        const list: Room[] = d.items ?? d ?? [];
        setRooms(list);
        if (list.length > 0) setRoomId(list[0].id);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!roomId) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/rooms/${roomId}/messages`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          content: text || entityTitle,
          content_type: "action",
          metadata: {
            entity_type: entityType,
            entity_id: entityId,
            title: entityTitle,
          },
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setDone(true);
      setTimeout(onClose, 900);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-background rounded-xl border border-border shadow-xl w-full max-w-sm mx-4 p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold">Поделиться в чат</h2>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground text-lg leading-none"
          >
            ✕
          </button>
        </div>

        {done ? (
          <p className="text-sm text-green-600 py-4 text-center">
            Отправлено в чат
          </p>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <div>
              <label className="text-xs text-muted-foreground block mb-1">
                Комната
              </label>
              <select
                value={roomId}
                onChange={(e) => setRoomId(e.target.value)}
                className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
              >
                {rooms.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name || `DM ${r.id.slice(0, 6)}`}
                  </option>
                ))}
                {rooms.length === 0 && (
                  <option value="">Нет доступных чатов</option>
                )}
              </select>
            </div>

            <div>
              <label className="text-xs text-muted-foreground block mb-1">
                Сообщение (необязательно)
              </label>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder={entityTitle}
                rows={2}
                className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background resize-none"
              />
            </div>

            {error && (
              <p className="text-xs text-destructive">Ошибка: {error}</p>
            )}

            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={onClose}
                className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted"
              >
                Отмена
              </button>
              <button
                type="submit"
                disabled={saving || !roomId}
                className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
              >
                {saving ? "Отправка..." : "Поделиться"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
