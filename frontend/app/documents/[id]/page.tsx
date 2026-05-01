"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { documents as docsApi } from "@/lib/api-client";
import { PdfViewer } from "@/components/review/pdf-viewer";
import { useTranslations } from "next-intl";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface DocumentDetail {
  id: string;
  file_name: string;
  file_hash: string;
  file_size: number;
  mime_type: string;
  page_count: number | null;
  doc_type: string | null;
  doc_type_confidence: number | null;
  status: string;
  source_channel: string | null;
  created_at: string;
  updated_at: string;
  extractions: Array<{
    id: string;
    model_name: string;
    overall_confidence: number | null;
    fields: Array<{
      field_name: string;
      field_value: string | null;
      confidence: number | null;
      confidence_reason: string | null;
      human_corrected: boolean;
    }>;
    created_at: string;
  }>;
  links: Array<{
    id: string;
    linked_entity_type: string;
    linked_entity_id: string;
    link_type: string;
  }>;
}

export default function DocumentPage() {
  const params = useParams();
  const router = useRouter();
  const tDoc = useTranslations("document");
  const tActions = useTranslations("actions");
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showRejectDialog, setShowRejectDialog] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  useEffect(() => {
    if (!params.id) return;
    fetch(`${API}/api/documents/${params.id}`)
      .then((r) => {
        if (!r.ok) throw new Error("Not found");
        return r.json();
      })
      .then(setDoc)
      .catch(() => setDoc(null))
      .finally(() => setLoading(false));
  }, [params.id]);

  // Keyboard shortcuts
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;
      if (e.key === "Escape") router.push("/inbox");
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [router]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-slate-400">
        Loading...
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="flex items-center justify-center h-full text-slate-400">
        Document not found
      </div>
    );
  }

  const latestExtraction = doc.extractions[0] ?? null;
  const isDecided = doc.status === "approved" || doc.status === "rejected";

  async function handleApprove() {
    if (!doc || saving || isDecided) return;
    setSaving(true);
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      await docsApi.update(doc.id, { status: "approved" } as any);
      router.push("/inbox");
    } catch {
      setSaving(false);
    }
  }

  async function handleReject() {
    if (!doc || saving || isDecided) return;
    setSaving(true);
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      await docsApi.update(doc.id, { status: "rejected" } as any);
      setShowRejectDialog(false);
      router.push("/inbox");
    } catch {
      setSaving(false);
    }
  }

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <button
            onClick={() => router.push("/inbox")}
            className="text-sm text-slate-400 hover:text-slate-600 mb-2"
          >
            &larr; Back to Inbox
          </button>
          <h1 className="text-xl font-bold">{doc.file_name}</h1>
          <div className="flex items-center gap-3 mt-1">
            <span
              className={`px-2 py-0.5 text-xs font-medium rounded-full ${
                doc.status === "needs_review"
                  ? "bg-amber-100 text-amber-700"
                  : doc.status === "approved"
                    ? "bg-green-100 text-green-700"
                    : doc.status === "rejected"
                      ? "bg-red-100 text-red-700"
                      : "bg-slate-100 text-slate-600"
              }`}
            >
              {tDoc(`status.${doc.status}`)}
            </span>
            {doc.doc_type && (
              <span className="text-sm text-slate-500">
                {tDoc(`type.${doc.doc_type}`)}
              </span>
            )}
            <span className="text-sm text-slate-400">
              {(doc.file_size / 1024).toFixed(0)} KB
            </span>
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-2">
          {latestExtraction && (
            <button
              onClick={() => router.push(`/documents/${params.id}/review`)}
              className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 font-medium"
            >
              {tActions("review") ?? "Проверить"}
            </button>
          )}
          <button
            onClick={handleApprove}
            disabled={saving || isDecided}
            className="px-3 py-1.5 text-sm bg-green-500 text-white rounded-md hover:bg-green-600 disabled:opacity-50"
          >
            {tActions("approve")}
          </button>
          <button
            onClick={() => setShowRejectDialog(true)}
            disabled={saving || isDecided}
            className="px-3 py-1.5 text-sm bg-red-500 text-white rounded-md hover:bg-red-600 disabled:opacity-50"
          >
            {tActions("reject")}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-6" style={{ minHeight: 600 }}>
        {/* PDF Viewer */}
        <div className="col-span-2" style={{ height: 640 }}>
          <PdfViewer
            documentId={doc.id}
            mimeType={doc.mime_type}
            highlightedBbox={null}
            bboxes={{}}
            activeField={null}
          />
        </div>

        {/* Sidebar — metadata + extraction */}
        <div className="space-y-4">
          {/* Metadata */}
          <div className="bg-white border border-slate-200 rounded-lg p-4">
            <h3 className="text-sm font-semibold mb-3">Metadata</h3>
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="text-slate-400">Hash</dt>
                <dd className="font-mono text-xs truncate">{doc.file_hash}</dd>
              </div>
              <div>
                <dt className="text-slate-400">Source</dt>
                <dd>{doc.source_channel ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-slate-400">Created</dt>
                <dd>{new Date(doc.created_at).toLocaleString("ru-RU")}</dd>
              </div>
              {doc.doc_type_confidence != null && (
                <div>
                  <dt className="text-slate-400">Classification confidence</dt>
                  <dd>{(doc.doc_type_confidence * 100).toFixed(0)}%</dd>
                </div>
              )}
            </dl>
          </div>

          {/* Extraction fields */}
          {latestExtraction && (
            <div className="bg-white border border-slate-200 rounded-lg p-4">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold">Extracted Fields</h3>
                {latestExtraction.overall_confidence != null && (
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full ${
                      latestExtraction.overall_confidence > 0.8
                        ? "bg-green-100 text-green-700"
                        : latestExtraction.overall_confidence > 0.5
                          ? "bg-amber-100 text-amber-700"
                          : "bg-red-100 text-red-700"
                    }`}
                  >
                    {(latestExtraction.overall_confidence * 100).toFixed(0)}%
                  </span>
                )}
              </div>
              <dl className="space-y-2 text-sm">
                {latestExtraction.fields.map((f) => (
                  <div
                    key={f.field_name}
                    className={`${f.confidence != null && f.confidence < 0.6 ? "bg-amber-50 -mx-2 px-2 py-1 rounded" : ""}`}
                  >
                    <dt className="text-slate-400 flex items-center gap-1">
                      {f.field_name}
                      {f.human_corrected && (
                        <span className="text-xs text-blue-500">
                          (corrected)
                        </span>
                      )}
                    </dt>
                    <dd className="font-medium">
                      {f.field_value ?? "—"}
                      {f.confidence != null && (
                        <span className="text-xs text-slate-400 ml-1">
                          ({(f.confidence * 100).toFixed(0)}%)
                        </span>
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          )}

          {/* Links */}
          {doc.links.length > 0 && (
            <div className="bg-white border border-slate-200 rounded-lg p-4">
              <h3 className="text-sm font-semibold mb-3">Links</h3>
              <ul className="space-y-1 text-sm">
                {doc.links.map((l) => (
                  <li key={l.id} className="text-slate-600">
                    {l.link_type}: {l.linked_entity_type}{" "}
                    {l.linked_entity_id.slice(0, 8)}...
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Reject dialog */}
      {showRejectDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-96 max-w-full mx-4 rounded-lg bg-white p-6 shadow-xl">
            <h3 className="mb-3 text-sm font-semibold text-slate-800">
              Причина отклонения
            </h3>
            <textarea
              autoFocus
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder="Укажите причину..."
              rows={3}
              className="w-full resize-none rounded border border-slate-200 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400"
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => setShowRejectDialog(false)}
                className="rounded border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
              >
                Отмена
              </button>
              <button
                onClick={handleReject}
                disabled={saving}
                className="rounded bg-red-500 px-3 py-1.5 text-sm text-white hover:bg-red-600 disabled:opacity-50"
              >
                Отклонить
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
