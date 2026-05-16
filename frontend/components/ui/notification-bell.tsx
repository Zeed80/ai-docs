"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { getApiBaseUrl, getWebSocketBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();
const WS_BASE = getWebSocketBaseUrl();

interface Notification {
  id: string;
  type: string;
  title: string;
  body: string;
  action_url: string | null;
  is_read: boolean;
  created_at: string;
}

export function NotificationBell() {
  const router = useRouter();
  const [count, setCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Notification[]>([]);
  const [loading, setLoading] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  function loadCount() {
    fetch(`${API}/api/notifications/unread-count`, { credentials: "include" })
      .then((r) => r.json())
      .then((d) => setCount(d.count ?? 0))
      .catch(() => {});
  }

  function loadItems() {
    setLoading(true);
    fetch(`${API}/api/notifications?limit=50`, { credentials: "include" })
      .then((r) => r.json())
      .then((d) => setItems(d.items ?? []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }

  // Poll every 30s as fallback
  useEffect(() => {
    loadCount();
    const t = setInterval(loadCount, 30_000);
    return () => clearInterval(t);
  }, []);

  // WebSocket for instant updates
  useEffect(() => {
    if (!WS_BASE) return;
    let ws: WebSocket;
    let reconnectTimeout: ReturnType<typeof setTimeout>;

    function connect() {
      try {
        ws = new WebSocket(`${WS_BASE}/api/notifications/ws`);
        wsRef.current = ws;

        ws.onmessage = (e) => {
          try {
            const event = JSON.parse(e.data);
            if (event.type === "notification") {
              setCount((c) => c + 1);
              if (open) {
                setItems((prev) => [event.data as Notification, ...prev]);
              }
            }
          } catch {
            /* ignore */
          }
        };

        ws.onclose = () => {
          reconnectTimeout = setTimeout(connect, 5000);
        };
      } catch {
        reconnectTimeout = setTimeout(connect, 5000);
      }
    }

    connect();
    return () => {
      clearTimeout(reconnectTimeout);
      ws?.close();
      wsRef.current = null;
    };
  }, [open]);

  // Close drawer on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  function toggle() {
    if (!open) loadItems();
    setOpen((v) => !v);
  }

  async function markRead(id: string) {
    setItems((prev) =>
      prev.map((n) => (n.id === id ? { ...n, is_read: true } : n)),
    );
    setCount((c) => Math.max(0, c - 1));
    await fetch(`${API}/api/notifications/${id}/read`, {
      method: "POST",
      credentials: "include",
      headers: csrfHeaders(),
    });
  }

  async function markAllRead() {
    setItems((prev) => prev.map((n) => ({ ...n, is_read: true })));
    setCount(0);
    await fetch(`${API}/api/notifications/read-all`, {
      method: "POST",
      credentials: "include",
      headers: csrfHeaders(),
    });
  }

  return (
    <div className="relative" ref={drawerRef}>
      <button
        onClick={toggle}
        title="Уведомления"
        className="relative p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
      >
        <svg
          className="w-4 h-4"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
          />
        </svg>
        {count > 0 && (
          <span className="absolute -top-0.5 -right-0.5 text-[9px] font-bold bg-red-500 text-white rounded-full w-3.5 h-3.5 flex items-center justify-center leading-none">
            {count > 9 ? "9+" : count}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute bottom-full right-0 mb-2 w-80 bg-popover border border-border rounded-lg shadow-xl z-50 flex flex-col max-h-[70vh]">
          <div className="flex items-center justify-between px-3 py-2 border-b border-border">
            <span className="text-xs font-semibold">Уведомления</span>
            {count > 0 && (
              <button
                onClick={markAllRead}
                className="text-[10px] text-muted-foreground hover:text-foreground transition-colors"
              >
                Прочитать все
              </button>
            )}
          </div>

          <div className="overflow-y-auto flex-1">
            {loading && (
              <p className="text-xs text-muted-foreground text-center py-4">
                Загрузка...
              </p>
            )}
            {!loading && items.length === 0 && (
              <p className="text-xs text-muted-foreground text-center py-6">
                Нет уведомлений
              </p>
            )}
            {items.map((n) => (
              <div
                key={n.id}
                onClick={() => {
                  if (!n.is_read) markRead(n.id);
                  if (n.action_url) {
                    if (n.action_url.startsWith("http")) {
                      window.open(n.action_url, "_blank");
                    } else {
                      router.push(n.action_url);
                    }
                  }
                  setOpen(false);
                }}
                className={`px-3 py-2.5 border-b border-border last:border-0 cursor-pointer hover:bg-muted transition-colors ${
                  n.is_read ? "opacity-60" : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  {!n.is_read && (
                    <div className="w-1.5 h-1.5 rounded-full bg-blue-500 mt-1.5 shrink-0" />
                  )}
                  {n.is_read && <div className="w-1.5 shrink-0" />}
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium leading-tight">
                      {n.title}
                    </p>
                    <p className="text-[11px] text-muted-foreground mt-0.5 leading-snug line-clamp-2">
                      {n.body}
                    </p>
                    <p className="text-[10px] text-muted-foreground/60 mt-1">
                      {new Date(n.created_at).toLocaleString("ru", {
                        day: "numeric",
                        month: "short",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
