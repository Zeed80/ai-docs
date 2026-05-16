"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();

interface Room {
  id: string;
  name: string;
  type: "direct" | "group" | "system";
  description: string | null;
  unread_count: number;
  last_message: string | null;
  last_message_at: string | null;
}

interface UserOut {
  sub: string;
  name: string;
  email: string;
}

function RoomTypeBadge({ type }: { type: Room["type"] }) {
  if (type === "direct")
    return (
      <span className="text-[10px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
        DM
      </span>
    );
  if (type === "system")
    return (
      <span className="text-[10px] text-blue-500 bg-blue-50 dark:bg-blue-950 px-1.5 py-0.5 rounded">
        Система
      </span>
    );
  return (
    <span className="text-[10px] text-green-600 bg-green-50 dark:bg-green-950 px-1.5 py-0.5 rounded">
      Группа
    </span>
  );
}

export default function ChatPage() {
  const router = useRouter();
  const [rooms, setRooms] = useState<Room[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNewGroup, setShowNewGroup] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");
  const [showNewDm, setShowNewDm] = useState(false);
  const [users, setUsers] = useState<UserOut[]>([]);
  const [creating, setCreating] = useState(false);

  function loadRooms() {
    fetch(`${API}/api/rooms`, { credentials: "include" })
      .then((r) => r.json())
      .then((d) => setRooms(d.items ?? []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadRooms();
  }, []);

  useEffect(() => {
    if (showNewDm) {
      fetch(`${API}/api/auth/users`, { credentials: "include" })
        .then((r) => r.json())
        .then((d) => setUsers(d.items ?? []))
        .catch(() => {});
    }
  }, [showNewDm]);

  async function createGroup() {
    if (!newGroupName.trim()) return;
    setCreating(true);
    try {
      const res = await fetch(`${API}/api/rooms`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ name: newGroupName.trim(), type: "group" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const room: Room = await res.json();
      setShowNewGroup(false);
      setNewGroupName("");
      router.push(`/chat/${room.id}`);
    } catch {
      /* ignore */
    } finally {
      setCreating(false);
    }
  }

  async function openDm(sub: string) {
    setCreating(true);
    try {
      const res = await fetch(
        `${API}/api/rooms/dm/${encodeURIComponent(sub)}`,
        {
          method: "GET",
          credentials: "include",
        },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const room: Room = await res.json();
      router.push(`/chat/${room.id}`);
    } catch {
      /* ignore */
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="p-6 max-w-2xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold">Чат</h1>
          <p className="text-sm text-muted-foreground">
            Командные комнаты и личные сообщения
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => {
              setShowNewDm(true);
              setShowNewGroup(false);
            }}
            className="px-3 py-1.5 text-sm border border-border rounded hover:bg-muted transition-colors"
          >
            Написать
          </button>
          <button
            onClick={() => {
              setShowNewGroup(true);
              setShowNewDm(false);
            }}
            className="px-3 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:bg-primary/90 transition-colors"
          >
            Новая группа
          </button>
        </div>
      </div>

      {showNewGroup && (
        <div className="rounded-lg border border-border bg-card p-4 mb-4 space-y-3">
          <h3 className="text-sm font-semibold">Создать группу</h3>
          <input
            type="text"
            autoFocus
            value={newGroupName}
            onChange={(e) => setNewGroupName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createGroup()}
            placeholder="Название группы..."
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          />
          <div className="flex gap-2">
            <button
              onClick={createGroup}
              disabled={creating || !newGroupName.trim()}
              className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
            >
              Создать
            </button>
            <button
              onClick={() => setShowNewGroup(false)}
              className="px-3 py-1.5 rounded border border-border text-sm"
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {showNewDm && (
        <div className="rounded-lg border border-border bg-card p-4 mb-4 space-y-2">
          <h3 className="text-sm font-semibold">Написать пользователю</h3>
          {users.length === 0 && (
            <p className="text-xs text-muted-foreground">Загрузка...</p>
          )}
          <div className="max-h-48 overflow-y-auto space-y-1">
            {users.map((u) => (
              <button
                key={u.sub}
                onClick={() => openDm(u.sub)}
                disabled={creating}
                className="w-full text-left flex items-center gap-2 px-3 py-1.5 rounded hover:bg-muted transition-colors text-sm disabled:opacity-50"
              >
                <span className="w-6 h-6 rounded-full bg-blue-600 flex items-center justify-center text-[10px] font-bold text-white shrink-0">
                  {(u.name[0] ?? "?").toUpperCase()}
                </span>
                <span className="flex-1 truncate">{u.name}</span>
                <span className="text-xs text-muted-foreground truncate">
                  {u.email}
                </span>
              </button>
            ))}
          </div>
          <button
            onClick={() => setShowNewDm(false)}
            className="text-xs text-muted-foreground hover:underline"
          >
            Отмена
          </button>
        </div>
      )}

      {loading && <p className="text-sm text-muted-foreground">Загрузка...</p>}

      <div className="space-y-1">
        {rooms.map((room) => (
          <Link
            key={room.id}
            href={`/chat/${room.id}`}
            className="flex items-center gap-3 px-4 py-3 rounded-lg hover:bg-muted transition-colors"
          >
            <div className="w-8 h-8 rounded-full bg-slate-600 flex items-center justify-center text-xs font-bold text-white shrink-0">
              {room.type === "direct"
                ? "DM"
                : (room.name[0]?.toUpperCase() ?? "?")}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium truncate">
                  {room.name || "Личный чат"}
                </span>
                <RoomTypeBadge type={room.type} />
              </div>
              {room.last_message && (
                <p className="text-xs text-muted-foreground truncate">
                  {room.last_message}
                </p>
              )}
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0">
              {room.last_message_at && (
                <span className="text-[10px] text-muted-foreground">
                  {new Date(room.last_message_at).toLocaleTimeString("ru", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
              )}
              {room.unread_count > 0 && (
                <span className="text-[10px] font-bold bg-red-500 text-white rounded-full px-1.5 py-0.5 leading-none">
                  {room.unread_count}
                </span>
              )}
            </div>
          </Link>
        ))}
        {!loading && rooms.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-8">
            Нет активных комнат. Создайте группу или напишите коллеге.
          </p>
        )}
      </div>
    </div>
  );
}
