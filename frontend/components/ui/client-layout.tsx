"use client";

import { AgentCanvas } from "@/components/canvas/agent-canvas";
import { SvetaPanel } from "@/components/chat/sveta-panel";
import { Sidebar } from "@/components/ui/sidebar";
import { CanvasProvider, useCanvas } from "@/lib/canvas-context";
import { ResizableLayout } from "@/components/ui/resizable-layout";

function LayoutInner({ children }: { children: React.ReactNode }) {
  const { isOpen } = useCanvas();
  return (
    <ResizableLayout
      sidebar={<Sidebar />}
      chat={<SvetaPanel />}
      canvas={<AgentCanvas />}
      canvasOpen={isOpen}
    >
      {children}
    </ResizableLayout>
  );
}

export function ClientLayout({ children }: { children: React.ReactNode }) {
  return (
    <CanvasProvider>
      <LayoutInner>{children}</LayoutInner>
    </CanvasProvider>
  );
}
