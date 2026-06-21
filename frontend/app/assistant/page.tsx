"use client";

import dynamic from "next/dynamic";

// Full-screen agent ("AI-DOCS") for mobile, where the desktop right-hand chat pane
// is hidden. WS/localStorage/client-only — never SSR.
const AssistantPanel = dynamic(
  () =>
    import("@/components/chat/assistant-panel").then((m) => ({
      default: m.AssistantPanel,
    })),
  { ssr: false, loading: () => <div className="h-full w-full bg-slate-800" /> },
);

export default function AssistantPage() {
  // Fill the area between the mobile top bar (3rem) and bottom nav (4rem) so the
  // composer stays visible. dvh accounts for the mobile browser's address bar.
  return (
    <div className="h-[calc(100dvh-7rem)] min-h-[360px] w-full">
      <AssistantPanel />
    </div>
  );
}
