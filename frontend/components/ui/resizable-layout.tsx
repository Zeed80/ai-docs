"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const SIDEBAR_DEFAULT = 192; // w-48
const CHAT_DEFAULT = 320; // w-80
const SIDEBAR_MIN = 140;
const SIDEBAR_MAX = 480;
const CHAT_MIN = 220;
const CHAT_MAX = 640;

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function usePersistentWidth(key: string, defaultValue: number) {
  const [width, setWidth] = useState(defaultValue);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(key);
      if (stored !== null) setWidth(clamp(Number(stored), 0, 9999));
    } catch {}
  }, [key]);

  const update = useCallback(
    (next: number) => {
      setWidth(next);
      try {
        localStorage.setItem(key, String(next));
      } catch {}
    },
    [key],
  );

  return [width, update] as const;
}

function DragHandle({
  onDrag,
  cursor,
}: {
  onDrag: (delta: number) => void;
  cursor: "col-resize";
}) {
  const dragging = useRef(false);
  const lastX = useRef(0);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = true;
      lastX.current = e.clientX;

      const onMove = (ev: MouseEvent) => {
        if (!dragging.current) return;
        const delta = ev.clientX - lastX.current;
        lastX.current = ev.clientX;
        onDrag(delta);
      };
      const onUp = () => {
        dragging.current = false;
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.body.style.cursor = cursor;
      document.body.style.userSelect = "none";
    },
    [onDrag, cursor],
  );

  return (
    <div
      onMouseDown={onMouseDown}
      className="w-1 shrink-0 bg-slate-700 hover:bg-blue-500 active:bg-blue-400 transition-colors cursor-col-resize"
      style={{ cursor }}
    />
  );
}

export function ResizableLayout({
  sidebar,
  chat,
  children,
}: {
  sidebar: React.ReactNode;
  chat: React.ReactNode;
  children: React.ReactNode;
}) {
  const [sidebarWidth, setSidebarWidth] = usePersistentWidth(
    "layout:sidebarWidth",
    SIDEBAR_DEFAULT,
  );
  const [chatWidth, setChatWidth] = usePersistentWidth(
    "layout:chatWidth",
    CHAT_DEFAULT,
  );

  const handleSidebarDrag = useCallback(
    (delta: number) => {
      setSidebarWidth(clamp(sidebarWidth + delta, SIDEBAR_MIN, SIDEBAR_MAX));
    },
    [sidebarWidth, setSidebarWidth],
  );

  const handleChatDrag = useCallback(
    (delta: number) => {
      setChatWidth(clamp(chatWidth - delta, CHAT_MIN, CHAT_MAX));
    },
    [chatWidth, setChatWidth],
  );

  return (
    <div className="flex h-screen overflow-hidden">
      <div style={{ width: sidebarWidth, flexShrink: 0 }}>{sidebar}</div>
      <DragHandle onDrag={handleSidebarDrag} cursor="col-resize" />
      <main className="flex-1 overflow-auto min-w-0">{children}</main>
      <DragHandle onDrag={handleChatDrag} cursor="col-resize" />
      <div style={{ width: chatWidth, flexShrink: 0 }}>{chat}</div>
    </div>
  );
}
