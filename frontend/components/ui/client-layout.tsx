"use client";

import { AuthProvider } from "@/lib/auth-context";
import { SvetaPanel } from "@/components/chat/sveta-panel";
import { Sidebar } from "@/components/ui/sidebar";
import { ResizableLayout } from "@/components/ui/resizable-layout";
import { CommandPalette } from "@/components/ui/command-palette";
import { useEffect, useState } from "react";

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
      <ResizableLayout sidebar={<Sidebar />} chat={<SvetaPanel />}>
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
      <LayoutInner>{children}</LayoutInner>
    </AuthProvider>
  );
}
