"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const SIDEBAR_DEFAULT = 192; // w-48
const CHAT_DEFAULT = 320; // w-80
const CANVAS_DEFAULT = 400;
const SIDEBAR_MIN = 140;
const SIDEBAR_MAX = 480;
const CHAT_MIN = 220;
const CHAT_MAX = 640;
const CANVAS_MIN = 280;
const CANVAS_MAX = 800;

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

  // Supports functional updater (prev => next) or plain number
  const update = useCallback(
    (nextOrFn: number | ((prev: number) => number)) => {
      setWidth((prev) => {
        const next = typeof nextOrFn === "function" ? nextOrFn(prev) : nextOrFn;
        try {
          localStorage.setItem(key, String(next));
        } catch {}
        return next;
      });
    },
    [key],
  );

  return [width, update] as const;
}

function DragHandle({
  onDragRef,
}: {
  onDragRef: React.RefObject<(delta: number) => void>;
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
        onDragRef.current?.(delta);
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
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [onDragRef],
  );

  return (
    <div
      onMouseDown={onMouseDown}
      className="w-1 shrink-0 bg-slate-700 hover:bg-blue-500 active:bg-blue-400 transition-colors"
      style={{ cursor: "col-resize" }}
    />
  );
}

export function ResizableLayout({
  sidebar,
  chat,
  canvas,
  canvasOpen,
  children,
}: {
  sidebar: React.ReactNode;
  chat: React.ReactNode;
  canvas?: React.ReactNode;
  canvasOpen?: boolean;
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
  const [canvasWidth, setCanvasWidth] = usePersistentWidth(
    "layout:canvasWidth",
    CANVAS_DEFAULT,
  );

  const sidebarDragRef = useRef<(delta: number) => void>(() => {});
  const chatDragRef = useRef<(delta: number) => void>(() => {});
  const canvasDragRef = useRef<(delta: number) => void>(() => {});

  sidebarDragRef.current = (delta: number) => {
    setSidebarWidth((prev) => clamp(prev + delta, SIDEBAR_MIN, SIDEBAR_MAX));
  };
  chatDragRef.current = (delta: number) => {
    setChatWidth((prev) => clamp(prev - delta, CHAT_MIN, CHAT_MAX));
  };
  canvasDragRef.current = (delta: number) => {
    setCanvasWidth((prev) => clamp(prev - delta, CANVAS_MIN, CANVAS_MAX));
  };

  return (
    <div className="flex h-screen overflow-hidden">
      <div style={{ width: sidebarWidth, flexShrink: 0 }}>{sidebar}</div>
      <DragHandle onDragRef={sidebarDragRef} />
      <main className="flex-1 overflow-auto min-w-0">{children}</main>
      {canvasOpen && canvas && (
        <>
          <DragHandle onDragRef={canvasDragRef} />
          <div style={{ width: canvasWidth, flexShrink: 0 }}>{canvas}</div>
        </>
      )}
      <DragHandle onDragRef={chatDragRef} />
      <div style={{ width: chatWidth, flexShrink: 0 }}>{chat}</div>
    </div>
  );
}
