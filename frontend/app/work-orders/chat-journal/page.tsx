"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { ActionJournal } from "@/components/chat/action-journal";

function JournalPage() {
  const runId = useSearchParams().get("run_id");
  return <main className="mx-auto max-w-4xl space-y-4 p-4">
    <h1 className="text-xl font-semibold">Журнал и сверка действий</h1>
    {runId && /^[0-9a-f-]{36}$/i.test(runId) ? <ActionJournal key={runId} runId={runId} /> : <p>Откройте журнал из нужного чата.</p>}
  </main>;
}

export default function Page() {
  return <Suspense fallback={<p>Загрузка…</p>}><JournalPage /></Suspense>;
}
