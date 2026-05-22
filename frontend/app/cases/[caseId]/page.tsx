"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch } from "@/lib/auth";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

const API = getApiBaseUrl();

interface CaseDoc {
  id: string;
  file_name: string;
  status: string;
  doc_type: string | null;
  added_at: string;
  added_by?: string;
}

interface TimelineEvent {
  id: string;
  event_type: string;
  actor: string;
  summary: string;
  timestamp: string;
}

interface ApprovalGate {
  id: string;
  action_type: string;
  status: string;
  requested_by: string;
  context: Record<string, unknown>;
  created_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
}

interface CaseDetail {
  id: string;
  title: string;
  customer: string | null;
  task_description: string | null;
  status: string;
  created_by: string;
  created_at: string;
  documents: CaseDoc[];
  timeline: TimelineEvent[];
  approval_gates: ApprovalGate[];
}

const DOC_STATUS_COLORS: Record<string, string> = {
  ingested: "bg-slate-700 text-slate-300",
  needs_review: "bg-amber-900/60 text-amber-300",
  approved: "bg-green-900/60 text-green-300",
  rejected: "bg-red-900/60 text-red-300",
  suspicious: "bg-red-900/80 text-red-200",
  quarantined: "bg-red-950 text-red-300 border border-red-700",
};

const APPROVAL_STATUS_COLORS: Record<string, string> = {
  pending: "bg-amber-900/50 text-amber-300",
  approved: "bg-green-900/50 text-green-300",
  rejected: "bg-red-900/50 text-red-300",
};

export default function CaseCockpitPage() {
  const params = useParams<{ caseId: string }>();
  const caseId = params.caseId;
  const router = useRouter();

  const [caseData, setCaseData] = useState<CaseDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [fileToAdd, setFileToAdd] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const res = await apiFetch(`${API}/api/cases/${caseId}`).catch(() => null);
    if (res?.ok) {
      setCaseData(await res.json());
    }
    setLoading(false);
  }, [caseId]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleAddDocument() {
    if (!fileToAdd) return;
    setUploading(true);
    setUploadError(null);
    try {
      const form = new FormData();
      form.append("file", fileToAdd);
      const uploadRes = await apiFetch(`${API}/api/documents/ingest`, {
        method: "POST",
        body: form,
      });
      if (!uploadRes.ok) {
        const err = await uploadRes.json().catch(() => ({}));
        setUploadError(err.detail ?? "Ошибка загрузки файла");
        return;
      }
      const uploaded = await uploadRes.json();
      const docId: string = uploaded.id ?? uploaded.document_id;

      const addRes = await apiFetch(`${API}/api/cases/${caseId}/documents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: docId }),
      });
      if (!addRes.ok) {
        const err = await addRes.json().catch(() => ({}));
        setUploadError(err.detail ?? "Ошибка добавления документа");
        return;
      }

      setFileToAdd(null);
      if (fileRef.current) fileRef.current.value = "";
      await load();
    } finally {
      setUploading(false);
    }
  }

  async function handleApprove(approvalId: string) {
    setDecidingId(approvalId);
    try {
      await apiFetch(
        `${API}/api/cases/${caseId}/approvals/${approvalId}/decide`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved: true }),
        },
      );
      await load();
    } finally {
      setDecidingId(null);
    }
  }

  async function handleReject(approvalId: string) {
    setDecidingId(approvalId);
    try {
      await apiFetch(
        `${API}/api/cases/${caseId}/approvals/${approvalId}/decide`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved: false }),
        },
      );
      await load();
    } finally {
      setDecidingId(null);
    }
  }

  const hasDangerousDocs = caseData?.documents.some(
    (d) => d.status === "suspicious" || d.status === "quarantined",
  );

  if (loading) {
    return (
      <div className="p-6 text-center text-slate-500 text-sm">Загрузка...</div>
    );
  }

  if (!caseData) {
    return (
      <div className="p-6 text-center text-red-400 text-sm">
        Кейс не найден.{" "}
        <button
          onClick={() => router.push("/cases")}
          className="underline text-blue-400"
        >
          Назад
        </button>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <button
          onClick={() => router.push("/cases")}
          className="text-xs text-slate-500 hover:text-slate-300 mb-2 block"
        >
          ← Все кейсы
        </button>
        <div className="flex items-start gap-3">
          <div className="flex-1">
            <h1 className="text-xl font-bold text-slate-100">
              {caseData.title}
            </h1>
            {caseData.customer && (
              <p className="text-sm text-slate-400 mt-0.5">
                {caseData.customer}
              </p>
            )}
            {caseData.task_description && (
              <p className="text-xs text-slate-500 mt-1 max-w-xl">
                {caseData.task_description}
              </p>
            )}
          </div>
          <span
            className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${
              caseData.status === "open"
                ? "bg-blue-900/40 text-blue-300"
                : "bg-slate-700 text-slate-400"
            }`}
          >
            {caseData.status}
          </span>
        </div>
      </div>

      {/* Add Document */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-4 space-y-3">
        <h2 className="text-sm font-semibold text-slate-200">
          Добавить документ
        </h2>
        <div className="flex gap-3 flex-wrap items-center">
          <input
            ref={fileRef}
            type="file"
            onChange={(e) => setFileToAdd(e.target.files?.[0] ?? null)}
            className="text-xs text-slate-400 file:mr-2 file:text-xs file:bg-slate-700 file:text-slate-200 file:border-0 file:px-3 file:py-1.5 file:rounded file:cursor-pointer"
          />
          <button
            onClick={handleAddDocument}
            disabled={!fileToAdd || uploading}
            className="px-4 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {uploading ? "Загружаю..." : "Добавить документ"}
          </button>
        </div>
        {uploadError && <p className="text-xs text-red-400">{uploadError}</p>}
      </section>

      {/* Documents */}
      {caseData.documents.length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-slate-200">
              Документы ({caseData.documents.length})
            </h2>
            <button
              disabled={!!hasDangerousDocs}
              className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Process
            </button>
          </div>
          <div className="space-y-2">
            {caseData.documents.map((doc) => (
              <div
                key={doc.id}
                className="flex items-center gap-3 p-3 bg-slate-800 border border-slate-700 rounded-lg"
              >
                <div className="flex-1 min-w-0">
                  <h3 className="text-sm font-medium text-slate-200 truncate">
                    {doc.file_name}
                  </h3>
                  <p className="text-[10px] text-slate-500 mt-0.5">
                    {new Date(doc.added_at).toLocaleString("ru-RU")}
                    {doc.doc_type && ` · ${doc.doc_type}`}
                  </p>
                </div>
                <span
                  className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${
                    DOC_STATUS_COLORS[doc.status] ??
                    "bg-slate-700 text-slate-300"
                  }`}
                >
                  {doc.status}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Approval Gates */}
      {caseData.approval_gates.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-slate-200 mb-3">
            Согласования
          </h2>
          <div className="space-y-2">
            {caseData.approval_gates.map((gate) => (
              <div
                key={gate.id}
                className="p-3 bg-slate-800 border border-slate-700 rounded-lg"
              >
                <div className="flex items-center gap-3 flex-wrap">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-slate-200">{gate.action_type}</p>
                    <p className="text-[10px] text-slate-500 mt-0.5">
                      Запросил: {gate.requested_by}
                      {gate.created_at &&
                        ` · ${new Date(gate.created_at).toLocaleString("ru-RU")}`}
                    </p>
                    {gate.decided_by && (
                      <p className="text-[10px] text-slate-500">
                        Решил: {gate.decided_by}
                        {gate.decided_at &&
                          ` · ${new Date(gate.decided_at).toLocaleString("ru-RU")}`}
                      </p>
                    )}
                  </div>
                  <span
                    className={`text-[10px] px-2 py-0.5 rounded-full ${
                      APPROVAL_STATUS_COLORS[gate.status] ??
                      "bg-slate-700 text-slate-400"
                    }`}
                  >
                    {gate.status}
                  </span>
                  {gate.status === "pending" && (
                    <div className="flex gap-2">
                      <button
                        onClick={() => handleApprove(gate.id)}
                        disabled={decidingId === gate.id}
                        className="px-3 py-1.5 text-xs bg-green-700 hover:bg-green-600 text-white rounded disabled:opacity-50"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => handleReject(gate.id)}
                        disabled={decidingId === gate.id}
                        className="px-3 py-1.5 text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 rounded disabled:opacity-50"
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Timeline */}
      {caseData.timeline.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-slate-200 mb-3">
            Хронология
          </h2>
          <div className="space-y-1.5">
            {caseData.timeline.map((ev) => (
              <div
                key={ev.id}
                className="flex items-start gap-3 text-xs py-1.5 border-b border-slate-800"
              >
                <span className="text-slate-500 shrink-0 pt-0.5">
                  {new Date(ev.timestamp).toLocaleTimeString("ru-RU", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
                <span className="font-mono text-slate-400 shrink-0">
                  {ev.event_type}
                </span>
                <span className="text-slate-300 flex-1">{ev.summary}</span>
                <span className="text-slate-600 shrink-0">{ev.actor}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
