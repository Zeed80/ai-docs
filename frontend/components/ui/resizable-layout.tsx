"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { BottomNav } from "@/components/mobile/BottomNav";

const SIDEBAR_DEFAULT = 192; // w-48
const CHAT_DEFAULT = 320; // w-80
const SIDEBAR_MIN = 140;
const SIDEBAR_MAX = 480;
const CHAT_MIN = 220;
const CHAT_MAX = 360;
const MAIN_MIN = 720;
const STACK_BREAKPOINT = 1280;

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

function useViewportWidth() {
  const [width, setWidth] = useState(1440);

  useEffect(() => {
    function update() {
      setWidth(window.innerWidth || 1440);
    }
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  return width;
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
  children,
}: {
  sidebar: React.ReactNode;
  chat: React.ReactNode;
  children: React.ReactNode;
}) {
  const viewportWidth = useViewportWidth();
  const pathname = usePathname();
  // Public / auth pages render full-screen without the app chrome.
  const isBare =
    !!pathname &&
    (pathname.startsWith("/auth/") || pathname.startsWith("/get-app"));
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Close the mobile drawer whenever the route changes.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);
  const [sidebarWidth, setSidebarWidth] = usePersistentWidth(
    "layout:sidebarWidth",
    SIDEBAR_DEFAULT,
  );
  const [chatWidth, setChatWidth] = usePersistentWidth(
    "layout:chatWidth",
    CHAT_DEFAULT,
  );

  const sidebarDragRef = useRef<(delta: number) => void>(() => {});
  const chatDragRef = useRef<(delta: number) => void>(() => {});
  const isStacked = viewportWidth < STACK_BREAKPOINT;
  const maxSidebarWidth = Math.min(
    SIDEBAR_MAX,
    Math.max(SIDEBAR_MIN, viewportWidth * 0.18),
  );
  const safeSidebarWidth = isStacked
    ? viewportWidth
    : clamp(sidebarWidth, SIDEBAR_MIN, maxSidebarWidth);
  const availableForChat = viewportWidth - safeSidebarWidth - MAIN_MIN - 8;
  const maxChatWidth = Math.min(
    CHAT_MAX,
    Math.max(
      CHAT_MIN,
      availableForChat > CHAT_MIN ? availableForChat : viewportWidth * 0.32,
    ),
  );
  const safeChatWidth = isStacked
    ? viewportWidth
    : clamp(chatWidth, CHAT_MIN, maxChatWidth);

  sidebarDragRef.current = (delta: number) => {
    setSidebarWidth((prev) =>
      clamp(prev + delta, SIDEBAR_MIN, maxSidebarWidth),
    );
  };
  chatDragRef.current = (delta: number) => {
    setChatWidth((prev) => clamp(prev - delta, CHAT_MIN, maxChatWidth));
  };

  useEffect(() => {
    if (!isStacked && chatWidth !== safeChatWidth) {
      setChatWidth(safeChatWidth);
    }
  }, [chatWidth, isStacked, safeChatWidth, setChatWidth]);

  useEffect(() => {
    if (!isStacked && sidebarWidth !== safeSidebarWidth) {
      setSidebarWidth(safeSidebarWidth);
    }
  }, [isStacked, safeSidebarWidth, setSidebarWidth, sidebarWidth]);

  if (isBare) {
    return <div className="min-h-screen">{children}</div>;
  }

  if (isStacked) {
    // Mobile / tablet / narrow window: single column with a top bar, the sidebar
    // as a slide-in drawer, and the bottom tab bar. The agent ("Света") lives on
    // its own route (/assistant) — the desktop right-hand chat pane is omitted.
    return (
      <div className="flex h-screen flex-col overflow-hidden">
        {/* Top app bar */}
        <header
          className="flex h-12 shrink-0 items-center gap-2 border-b border-slate-700 bg-slate-900 px-3"
          style={{ paddingTop: "env(safe-area-inset-top)" }}
        >
          <button
            type="button"
            aria-label="Меню"
            onClick={() => setDrawerOpen(true)}
            className="rounded p-1.5 text-slate-300 hover:bg-slate-700"
          >
            <svg
              className="h-6 w-6"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.8}
                d="M4 6h16M4 12h16M4 18h16"
              />
            </svg>
          </button>
          <span className="text-sm font-semibold text-slate-100">AI-DOCS</span>
        </header>

        {/* Content */}
        <main className="min-h-0 flex-1 overflow-auto pb-[calc(4rem+env(safe-area-inset-bottom))]">
          {children}
        </main>

        {/* Slide-in sidebar drawer */}
        {drawerOpen && (
          <div className="fixed inset-0 z-50 flex">
            <div
              className="absolute inset-0 bg-black/50"
              onClick={() => setDrawerOpen(false)}
            />
            <div className="relative z-10 h-full w-72 max-w-[80vw] shadow-xl">
              {sidebar}
            </div>
          </div>
        )}

        <BottomNav />
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <div style={{ width: safeSidebarWidth, flexShrink: 0 }}>{sidebar}</div>
      <DragHandle onDragRef={sidebarDragRef} />
      <main className="flex-1 overflow-auto min-w-0">{children}</main>
      <DragHandle onDragRef={chatDragRef} />
      <div style={{ width: safeChatWidth, flexShrink: 0 }}>{chat}</div>
    </div>
  );
}
