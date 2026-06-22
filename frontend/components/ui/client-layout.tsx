"use client";

import dynamic from "next/dynamic";
import { AuthProvider } from "@/lib/auth-context";
import { AgentNameProvider } from "@/lib/agent-name";
import { Sidebar } from "@/components/ui/sidebar";
import { ResizableLayout } from "@/components/ui/resizable-layout";
import { CommandPalette } from "@/components/ui/command-palette";
import { useEffect, useState } from "react";

// AssistantPanel uses WebSocket, localStorage, and client-only state — never SSR it.
// ssr: false eliminates hydration mismatches on disabled/placeholder attributes.
const AssistantPanel = dynamic(
  () =>
    import("@/components/chat/assistant-panel").then((m) => ({
      default: m.AssistantPanel,
    })),
  {
    ssr: false,
    loading: () => <div className="relative w-full h-full bg-slate-800" />,
  },
);

function LayoutInner({ children }: { children: React.ReactNode }) {
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
      if (e.key === "Escape") setPaletteOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <>
      <ResizableLayout sidebar={<Sidebar />} chat={<AssistantPanel />}>
        {children}
      </ResizableLayout>
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
      />
    </>
  );
}

export function ClientLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AgentNameProvider>
        <LayoutInner>{children}</LayoutInner>
      </AgentNameProvider>
    </AuthProvider>
  );
}
