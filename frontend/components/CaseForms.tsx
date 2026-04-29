"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import {
  analyzeDrawing,
  createDocumentDownloadUrl,
  createCase,
  extractInvoice,
  processDocument,
  runTask,
  runAgentScenario,
  uploadDocument,
  type ApprovalGate,
  type TaskJob,
  type WorkspaceDocument,
  approveGate,
  rejectGate,
} from "../lib/api";

export function NewCaseForm() {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <form
      className="upload-card"
      onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        const form = new FormData(event.currentTarget);
        startTransition(async () => {
          try {
            const item = await createCase({
              title: String(form.get("title") ?? ""),
              customer_name: String(form.get("customer") ?? ""),
              description: String(form.get("description") ?? ""),
              priority: "normal",
            });
            router.push(`/cases/${item.id}`);
          } catch (caught) {
            setError(caught instanceof Error ? caught.message : "Не удалось создать кейс");
          }
        });
      }}
    >
      <p className="eyebrow">Быстрый старт</p>
      <input className="upload-input" name="title" placeholder="Название кейса: Вал Ø25 / счет Hoffmann" required />
      <input className="upload-input" name="customer" placeholder="Заказчик" />
      <textarea className="upload-input" name="description" placeholder="Что нужно сделать технологу?" rows={4} />
      <button className="action-button" disabled={pending} type="submit">
        {pending ? "Создаю..." : "Создать кейс"}
      </button>
      {error ? <p className="small-muted">{error}</p> : null}
    </form>
  );
}

export function UploadCard({ caseId }: { caseId: string }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <form
      className="upload-card"
      onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        const input = event.currentTarget.elements.namedItem("file") as HTMLInputElement;
        const file = input.files?.[0];
        if (!file) {
          setError("Выберите файл");
          return;
        }
        startTransition(async () => {
          try {
            await uploadDocument(caseId, file);
            router.refresh();
          } catch (caught) {
            setError(caught instanceof Error ? caught.message : "Upload failed");
          }
        });
      }}
    >
      <p className="eyebrow">Upload</p>
      <input className="upload-input" name="file" type="file" />
      <button className="action-button" disabled={pending} type="submit">
        {pending ? "Загружаю..." : "Добавить документ"}
      </button>
      {error ? <p className="small-muted">{error}</p> : null}
    </form>
  );
}

export function DocumentActions({ document }: { document: WorkspaceDocument }) {
  const router = useRouter();
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  async function run(label: string, action: () => Promise<unknown>) {
    setPendingAction(label);
    try {
      await action();
      router.refresh();
    } finally {
      setPendingAction(null);
    }
  }

  async function download() {
    setPendingAction("download");
    try {
      const signed = await createDocumentDownloadUrl(document.id);
      window.location.href = signed.url;
    } finally {
      setPendingAction(null);
    }
  }

  return (
    <div className="action-row">
      <button
        className="ghost-button"
        disabled={pendingAction !== null || document.status === "suspicious"}
        onClick={() => run("process", () => processDocument(document.id))}
        type="button"
      >
        {pendingAction === "process" ? "В очередь..." : "Process"}
      </button>
      <button className="ghost-button" disabled={pendingAction !== null} onClick={() => run("drawing", () => analyzeDrawing(document.id))} type="button">
        Чертеж
      </button>
      <button className="ghost-button" disabled={pendingAction !== null} onClick={() => run("invoice", () => extractInvoice(document.id))} type="button">
        Счет
      </button>
      <button className="ghost-button" disabled={pendingAction !== null} onClick={download} type="button">
        Скачать
      </button>
    </div>
  );
}

export function TaskActions({ task }: { task: TaskJob }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const runnable = task.status === "pending" || task.status === "retry_scheduled";
  return (
    <button
      className="ghost-button"
      disabled={pending || !runnable}
      onClick={() => {
        startTransition(async () => {
          await runTask(task.id);
          router.refresh();
        });
      }}
      type="button"
    >
      {pending ? "Запускаю..." : runnable ? "Run" : task.status}
    </button>
  );
}

export function ApprovalActions({ gate }: { gate: ApprovalGate }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  if (gate.status !== "pending") {
    return <span className="status-pill">{gate.status}</span>;
  }
  return (
    <div className="action-row">
      <button
        className="ghost-button"
        disabled={pending}
        onClick={() => {
          startTransition(async () => {
            await approveGate(gate.id, "Approved from cockpit");
            router.refresh();
          });
        }}
        type="button"
      >
        Approve
      </button>
      <button
        className="ghost-button"
        disabled={pending}
        onClick={() => {
          startTransition(async () => {
            await rejectGate(gate.id, "Rejected from cockpit");
            router.refresh();
          });
        }}
        type="button"
      >
        Reject
      </button>
    </div>
  );
}

export function ScenarioActions({ caseId }: { caseId: string }) {
  const router = useRouter();
  const [pendingScenario, setPendingScenario] = useState<string | null>(null);
  const scenarios = ["smart_ingest", "drawing_review", "draft_email"];

  return (
    <div className="action-row">
      {scenarios.map((scenario) => (
        <button
          className="ghost-button"
          disabled={pendingScenario !== null}
          key={scenario}
          onClick={async () => {
            setPendingScenario(scenario);
            try {
              await runAgentScenario(caseId, scenario);
              router.refresh();
            } finally {
              setPendingScenario(null);
            }
          }}
          type="button"
        >
          {pendingScenario === scenario ? "Запуск..." : scenario}
        </button>
      ))}
    </div>
  );
}
