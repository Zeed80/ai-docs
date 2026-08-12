"use client";

import dynamic from "next/dynamic";
import { usePathname } from "next/navigation";
import { AuthProvider } from "@/lib/auth-context";
import { AgentNameProvider } from "@/lib/agent-name";
import { Sidebar } from "@/components/ui/sidebar";
import { ResizableLayout } from "@/components/ui/resizable-layout";
import { CommandPalette } from "@/components/ui/command-palette";
import { SectionGuard } from "@/components/auth/section-guard";
import { useEffect, useState } from "react";

// A route that owns the whole viewport — the CAD editor most of all: a
// tiny embedded 3D viewport squeezed next to a chat panel is the opposite
// of what a person doing real geometry work needs. Matches
// /cad/<id>/editor exactly (not /cad or /cad/<id> — those keep the normal
// chrome; only the dedicated editor route goes full-screen). The optional
// "2" also matches /cad/<id>/editor2 — the new ribbon editor's temporary
// development route (see /root/.claude/plans/starry-mapping-hippo.md);
// drop the "2?" once editor2 is promoted to editor/ in that plan's Фаза 5.
const CHROMELESS_ROUTE = /^\/cad\/[^/]+\/editor2?(\/|$)/;

function isChromeless(pathname: string | null): boolean {
  return !!pathname && CHROMELESS_ROUTE.test(pathname);
}

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
  const pathname = usePathname();
  const chromeless = isChromeless(pathname);

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

  // Auth/access control (SectionGuard) still applies — only the sidebar,
  // resizable split and chat panel are skipped. Command palette (Ctrl+K)
  // stays available everywhere, the editor included.
  if (chromeless) {
    return (
      <>
        <SectionGuard>{children}</SectionGuard>
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
        />
      </>
    );
  }

  return (
    <>
      <ResizableLayout sidebar={<Sidebar />} chat={<AssistantPanel />}>
        <SectionGuard>{children}</SectionGuard>
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
