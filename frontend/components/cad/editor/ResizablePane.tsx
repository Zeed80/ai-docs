"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Ф10: a single draggable-width side pane, matching the drag-handle
 * technique components/ui/resizable-layout.tsx already uses (mouse-down →
 * document-level move/up listeners, localStorage-persisted width) — but
 * that component itself isn't reusable here as-is: it's shaped for the
 * WHOLE APP's sidebar+chat+mobile-drawer chrome (BottomNav, /auth/ bare-
 * mode, two fixed panes named "sidebar"/"chat"), not a generic primitive.
 * This is the same TECHNIQUE, built fresh for one editor-owned pane. */

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function usePersistentWidth(
  key: string,
  defaultValue: number,
  min: number,
  max: number,
) {
  const [width, setWidth] = useState(defaultValue);
  useEffect(() => {
    try {
      const stored = localStorage.getItem(key);
      if (stored !== null) setWidth(clamp(Number(stored), min, max));
    } catch {
      // localStorage can be unavailable (private mode, SSR) — the default
      // width is a perfectly fine fallback, not an error.
    }
    // Only read once on mount — min/max changing shouldn't re-trigger a
    // fresh localStorage read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const update = useCallback(
    (next: number) => {
      const clamped = clamp(next, min, max);
      setWidth(clamped);
      try {
        localStorage.setItem(key, String(clamped));
      } catch {
        // Best-effort persistence only.
      }
    },
    [key, min, max],
  );
  return [width, update] as const;
}

export default function ResizablePane({
  storageKey,
  defaultWidth,
  minWidth,
  maxWidth,
  side,
  children,
}: {
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  // Which edge the drag handle sits on — "left" pane's handle is on its
  // right edge (dragging right grows it), "right" pane's on its left edge
  // (dragging left grows it).
  side: "left" | "right";
  children: React.ReactNode;
}) {
  const [width, setWidth] = usePersistentWidth(
    storageKey,
    defaultWidth,
    minWidth,
    maxWidth,
  );
  const dragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(width);

  const onMouseDown = useCallback(
    (event: React.MouseEvent) => {
      event.preventDefault();
      dragging.current = true;
      startX.current = event.clientX;
      startWidth.current = width;
      const onMove = (ev: MouseEvent) => {
        if (!dragging.current) return;
        const delta = ev.clientX - startX.current;
        setWidth(startWidth.current + (side === "left" ? delta : -delta));
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
    [side, width, setWidth],
  );

  const handle = (
    <div
      onMouseDown={onMouseDown}
      title="Потяните, чтобы изменить ширину панели"
      className="w-1 shrink-0 cursor-col-resize bg-white/5 transition-colors hover:bg-sky-500/50 active:bg-sky-400"
    />
  );

  return (
    <>
      {side === "right" && handle}
      <div style={{ width, flexShrink: 0 }} className="min-w-0">
        {children}
      </div>
      {side === "left" && handle}
    </>
  );
}
