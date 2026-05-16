"use client";

import { AuthProvider } from "@/lib/auth-context";
import { SvetaPanel } from "@/components/chat/sveta-panel";
import { Sidebar } from "@/components/ui/sidebar";
import { ResizableLayout } from "@/components/ui/resizable-layout";

function LayoutInner({ children }: { children: React.ReactNode }) {
  return (
    <ResizableLayout sidebar={<Sidebar />} chat={<SvetaPanel />}>
      {children}
    </ResizableLayout>
  );
}

export function ClientLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <LayoutInner>{children}</LayoutInner>
    </AuthProvider>
  );
}
