"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { takePendingShare } from "@/lib/mobile-share-store";
import { documents } from "@/lib/api-client";
import { emailApi } from "@/app/email/_components/api";

const DOC_TYPES = [
  { value: "", label: "Определить автоматически" },
  { value: "invoice", label: "Счёт" },
  { value: "letter", label: "Письмо" },
  { value: "contract", label: "Договор" },
  { value: "commercial_offer", label: "КП" },
  { value: "act", label: "Акт" },
  { value: "waybill", label: "Накладная" },
  { value: "drawing", label: "Чертёж" },
  { value: "other", label: "Прочее" },
];

/** Confirmation screen for files shared into the app from other apps. */
export default function SharePage() {
  const router = useRouter();
  const [files, setFiles] = useState<File[]>([]);
  const [docType, setDocType] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Ф7.3 — sharing a file into the app always meant "загрузить как документ".
  // Attaching it to a letter is just as common on a phone (a photo of a signed
  // act going straight back to the supplier) and had no path at all.
  const [target, setTarget] = useState<"document" | "email">("document");

  useEffect(() => {
    const shared = takePendingShare();
    if (!shared || !shared.files.length) {
      router.replace("/documents");
      return;
    }
    setFiles(shared.files);
  }, [router]);

  async function attachToEmail() {
    setBusy(true);
    setError(null);
    try {
      const ids: string[] = [];
      for (const f of files) {
        const staged = await emailApi.uploadAttachment(f);
        ids.push(staged.id);
      }
      // The composer reads these on mount; sessionStorage because the payload
      // is per-tab and must not outlive the flow.
      sessionStorage.setItem("email:pending-attachments", JSON.stringify(ids));
      router.replace("/email?compose=1");
    } catch (e) {
      console.error(e);
      setError("Не удалось приложить к письму. Попробуйте ещё раз.");
      setBusy(false);
    }
  }

  async function submit() {
    if (busy || !files.length) return;
    if (target === "email") {
      await attachToEmail();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      let firstId: string | undefined;
      for (const f of files) {
        const res = await documents.ingest(f, "mobile_share", {
          requestedDocType: docType || undefined,
        });
        firstId = firstId ?? (res?.id as string | undefined);
      }
      router.replace(firstId ? `/documents/${firstId}` : "/documents");
    } catch (e) {
      console.error(e);
      setError("Не удалось загрузить. Попробуйте ещё раз.");
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col gap-4 p-4">
      <h1 className="text-lg font-semibold text-slate-100">
        Загрузить в AI-DOCS
      </h1>

      <ul className="divide-y divide-slate-700 rounded-lg border border-slate-700 bg-slate-800/50">
        {files.map((f, i) => (
          <li key={i} className="flex items-center gap-3 p-3 text-sm">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-slate-700 text-slate-300">
              📄
            </span>
            <span className="flex-1 truncate text-slate-200">{f.name}</span>
            <span className="text-xs text-slate-400">
              {(f.size / 1024).toFixed(0)} КБ
            </span>
          </li>
        ))}
        {!files.length && (
          <li className="p-3 text-sm text-slate-400">Нет файлов</li>
        )}
      </ul>

      <div className="flex gap-2">
        {(
          [
            ["document", "Загрузить как документ"],
            ["email", "Приложить к письму"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setTarget(value)}
            className={`flex-1 rounded-lg border px-3 py-2 text-sm ${
              target === value
                ? "border-blue-500 bg-blue-600/20 text-blue-200"
                : "border-slate-700 bg-slate-800 text-slate-300"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {target === "document" && (
      <label className="text-sm text-slate-300">
        Тип документа
        <select
          value={docType}
          onChange={(e) => setDocType(e.target.value)}
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100"
        >
          {DOC_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      )}

      {error && <p className="text-sm text-red-400">{error}</p>}

      <div className="mt-auto flex gap-3 pb-[env(safe-area-inset-bottom)]">
        <button
          type="button"
          onClick={() => router.replace("/documents")}
          className="flex-1 rounded-lg border border-slate-700 py-2.5 text-sm text-slate-300"
        >
          Отмена
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={busy || !files.length}
          className="flex-1 rounded-lg bg-sky-500 py-2.5 text-sm font-medium text-white disabled:opacity-60"
        >
          {busy ? "Загрузка…" : "Загрузить"}
        </button>
      </div>
    </div>
  );
}
