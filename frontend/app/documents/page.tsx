"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

type WorkspaceTab = "upload" | "registry" | "queue" | "graph" | "ntd";

interface DocumentItem {
  id: string;
  file_name: string;
  file_hash: string;
  file_size: number;
  mime_type: string;
  storage_path?: string;
  page_count?: number | null;
  doc_type: string | null;
  doc_type_confidence: number | null;
  status: string;
  source_channel: string | null;
  created_at: string;
  updated_at: string;
}

interface PipelineStatus {
  processing_status: string | null;
  current_step: string | null;
  processing_error: string | null;
  pipeline_steps: PipelineStep[];
  extraction_count: number;
  artifact_count: number;
  graph_status: string | null;
  graph_scope: string | null;
  graph_error: string | null;
  memory_chunks: number;
  evidence_spans: number;
  graph_nodes: number;
  graph_edges: number;
  graph_review_pending: number;
  embedding_records: number;
  ntd_checks: number;
  ntd_open_findings: number;
}

interface PipelineStep {
  key: string;
  label: string;
  status:
    | "pending"
    | "queued"
    | "running"
    | "done"
    | "failed"
    | "skipped"
    | string;
  error?: string;
}

interface WorkspaceItem {
  document: DocumentItem;
  pipeline: PipelineStatus;
}

interface WorkspaceResponse {
  items: WorkspaceItem[];
  total: number;
  offset: number;
  limit: number;
  status_counts: Record<string, number>;
  doc_type_counts: Record<string, number>;
}

interface ManagementSummary {
  document: DocumentItem;
  pipeline: PipelineStatus;
  links: DocumentLink[];
}

interface DocumentLink {
  id: string;
  linked_entity_type: string;
  linked_entity_id: string;
  link_type: string;
}

interface DependenciesSummary {
  nodes: Array<{
    id: string;
    node_type: string;
    title: string;
    summary: string | null;
    confidence: number;
  }>;
  edges: Array<{
    id: string;
    source_node_id: string;
    target_node_id: string;
    edge_type: string;
    confidence: number;
    reason: string | null;
  }>;
  total_nodes: number;
  total_edges: number;
}

interface UploadResult {
  fileName: string;
  status: "uploaded" | "duplicate" | "quarantined" | "failed";
  detail: string;
}

type PendingFileStatus =
  | "pending"
  | "uploading"
  | "done"
  | "duplicate"
  | "quarantined"
  | "error";

interface PendingFile {
  id: string;
  file: File;
  /** Type guessed from file extension — shown immediately */
  guessedType: string;
  /** Type the user confirmed (starts as guessedType, editable) */
  confirmedType: string;
  status: PendingFileStatus;
  /** Actual type returned by backend after upload */
  detectedType?: string;
  detectedTypeSource?: string;
  detail?: string;
  documentId?: string;
}

interface SearchDocument {
  id: string;
  file_name: string;
  doc_type: string | null;
  status: string;
}

const TABS: Array<{ key: WorkspaceTab; label: string }> = [
  { key: "upload", label: "Загрузка" },
  { key: "registry", label: "Реестр" },
  { key: "queue", label: "Обработка" },
  { key: "graph", label: "Связи и граф" },
  { key: "ntd", label: "НТД" },
];

const STATUS_FILTERS = [
  { value: "", label: "Все статусы" },
  { value: "ingested", label: "Загружены" },
  { value: "classifying", label: "Классификация" },
  { value: "extracting", label: "Распознавание" },
  { value: "needs_review", label: "На проверку" },
  { value: "approved", label: "Утверждены" },
  { value: "rejected", label: "Отклонены" },
  { value: "suspicious", label: "Карантин" },
  { value: "archived", label: "Архив" },
];

const DOC_TYPES = [
  { value: "", label: "Автоопределение" },
  { value: "invoice", label: "Счет" },
  { value: "letter", label: "Письмо" },
  { value: "contract", label: "Договор" },
  { value: "drawing", label: "Чертеж" },
  { value: "commercial_offer", label: "КП" },
  { value: "act", label: "Акт" },
  { value: "waybill", label: "Накладная" },
  { value: "other", label: "Другое" },
];

/** Extension → doc_type mapping (client-side, no AI) */
const EXT_TYPE_MAP: Record<string, string> = {
  dwg: "drawing",
  dxf: "drawing",
  svg: "drawing",
  step: "drawing",
  stp: "drawing",
  iges: "drawing",
  igs: "drawing",
  xlsx: "invoice",
  xls: "invoice",
  docx: "letter",
  doc: "letter",
  odt: "letter",
};

function guessDocType(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  return EXT_TYPE_MAP[ext] ?? "";
}

const TYPE_SOURCE_LABEL: Record<string, string> = {
  extension: "расширение",
  mime: "MIME",
  suggested: "подсказка",
  manual: "вручную",
  ai: "ИИ",
};

const PIPELINE_STEP_LABELS: Record<string, string> = {
  store: "Файл",
  memory_seed: "Память",
  classification: "Класс",
  extraction: "OCR",
  sql_records: "SQL",
  memory_graph: "Граф",
  embedding: "Векторы",
};

const FALLBACK_PROCESS_STEPS: PipelineStep[] = [
  { key: "store", label: "Файл", status: "pending" },
  { key: "memory_seed", label: "Память", status: "pending" },
  { key: "classification", label: "Класс", status: "pending" },
  { key: "extraction", label: "OCR", status: "pending" },
  { key: "sql_records", label: "SQL", status: "pending" },
  { key: "memory_graph", label: "Граф", status: "pending" },
  { key: "embedding", label: "Векторы", status: "pending" },
];

function fmtBytes(value: number) {
  if (value > 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value > 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

function docTypeLabel(value: string | null | undefined) {
  return (
    DOC_TYPES.find((item) => item.value === value)?.label ?? value ?? "Не задан"
  );
}

function statusLabel(value: string | null | undefined) {
  return (
    STATUS_FILTERS.find((item) => item.value === value)?.label ??
    value ??
    "Не задан"
  );
}

function pipelineSteps(
  pipeline: PipelineStatus | null | undefined,
): PipelineStep[] {
  const steps = pipeline?.pipeline_steps?.length
    ? pipeline.pipeline_steps
    : FALLBACK_PROCESS_STEPS;
  return steps
    .filter((step) => step.key !== "ntd")
    .map((step) => ({
      ...step,
      label: PIPELINE_STEP_LABELS[step.key] ?? step.label ?? step.key,
    }));
}

function pipelineProgress(pipeline: PipelineStatus | null | undefined) {
  const steps = pipelineSteps(pipeline);
  if (!steps.length) return 0;
  const completed = steps.filter((step) =>
    ["done", "skipped"].includes(step.status),
  ).length;
  return Math.round((completed / steps.length) * 100);
}

function isPipelineActive(item: WorkspaceItem) {
  return (
    ["queued", "running"].includes(item.pipeline.processing_status ?? "") ||
    ["ingested", "classifying", "extracting"].includes(item.document.status)
  );
}

function useLocalStorage<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(initial);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(key);
      if (stored !== null) setValue(JSON.parse(stored) as T);
    } catch {}
  }, [key]);

  const set = useCallback(
    (v: T) => {
      setValue(v);
      try {
        localStorage.setItem(key, JSON.stringify(v));
      } catch {}
    },
    [key],
  );
  return [value, set];
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(body || `HTTP ${response.status}`);
  }
  return response.json();
}

export default function DocumentsPage() {
  const [tab, setTab] = useState<WorkspaceTab>("upload");
  const [workspace, setWorkspace] = useState<WorkspaceResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [summary, setSummary] = useState<ManagementSummary | null>(null);
  const [dependencies, setDependencies] = useState<DependenciesSummary | null>(
    null,
  );
  const [status, setStatus] = useState("");
  const [docType, setDocType] = useState("");
  const [search, setSearch] = useState("");
  const [sourceChannel, setSourceChannel] = useState("upload");
  const [uploadDocType, setUploadDocType] = useLocalStorage(
    "upload.docType",
    "",
  );
  const [autoProcess, setAutoProcess] = useLocalStorage(
    "upload.autoProcess",
    true,
  );
  const [autoVerify, setAutoVerify] = useLocalStorage(
    "upload.autoVerify",
    false,
  );
  const [manualUploadType, setManualUploadType] = useLocalStorage(
    "upload.manualType",
    false,
  );
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [dependencyQuery, setDependencyQuery] = useState("");
  const [linkType, setLinkType] = useState("related");
  const [targetQuery, setTargetQuery] = useState("");
  const [targetDocumentId, setTargetDocumentId] = useState("");
  const [targetSearchResults, setTargetSearchResults] = useState<
    SearchDocument[]
  >([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const selectedItem = useMemo(
    () =>
      workspace?.items.find((item) => item.document.id === selectedId) ?? null,
    [selectedId, workspace],
  );
  const selected = summary?.document ?? selectedItem?.document ?? null;
  const pipeline = summary?.pipeline ?? selectedItem?.pipeline ?? null;
  const selectedIdsArray = useMemo(
    () => Array.from(selectedIds),
    [selectedIds],
  );

  const loadWorkspace = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    if (docType) params.set("doc_type", docType);
    if (sourceChannel.trim())
      params.set("source_channel", sourceChannel.trim());
    if (search.trim()) params.set("search", search.trim());
    const data = await requestJson<WorkspaceResponse>(
      `/api/documents/workspace?${params}`,
    ).catch(() => null);
    if (!data) {
      setWorkspace(null);
      return;
    }
    setWorkspace(data);
    setSelectedId((current) => current ?? data.items[0]?.document.id ?? null);
  }, [docType, search, sourceChannel, status]);

  const loadSummary = useCallback(async (id: string | null) => {
    if (!id) {
      setSummary(null);
      return;
    }
    const data = await requestJson<ManagementSummary>(
      `/api/documents/${id}/management`,
    ).catch(() => null);
    setSummary(data);
  }, []);

  const loadDependencies = useCallback(
    async (id: string | null) => {
      if (!id) {
        setDependencies(null);
        return;
      }
      const params = new URLSearchParams({ depth: "2", limit: "150" });
      if (dependencyQuery.trim()) params.set("query", dependencyQuery.trim());
      const data = await requestJson<DependenciesSummary>(
        `/api/documents/${id}/dependencies?${params}`,
      ).catch(() => null);
      setDependencies(data);
    },
    [dependencyQuery],
  );

  useEffect(() => {
    loadWorkspace();
  }, [loadWorkspace]);

  useEffect(() => {
    const hasActivePipeline = Boolean(workspace?.items.some(isPipelineActive));
    if (!hasActivePipeline && !uploading) return;
    const timer = window.setInterval(() => {
      loadWorkspace();
      loadSummary(selectedId);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [loadSummary, loadWorkspace, selectedId, uploading, workspace]);

  useEffect(() => {
    loadSummary(selectedId);
    loadDependencies(selectedId);
  }, [loadDependencies, loadSummary, selectedId]);

  useEffect(() => {
    if (!targetQuery.trim()) {
      setTargetSearchResults([]);
      return;
    }
    const timer = window.setTimeout(async () => {
      const params = new URLSearchParams({
        limit: "10",
        search: targetQuery.trim(),
      });
      const data = await requestJson<{ items: SearchDocument[] }>(
        `/api/documents?${params}`,
      ).catch(() => null);
      setTargetSearchResults(data?.items ?? []);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [targetQuery]);

  async function refreshSelected() {
    await loadWorkspace();
    await loadSummary(selectedId);
    await loadDependencies(selectedId);
  }

  /** Add files to the upload queue with extension-based type pre-detection. */
  function addFilesToQueue(files: FileList | File[] | null) {
    if (!files?.length) return;
    const entries: PendingFile[] = Array.from(files).map((file) => {
      const guessed = guessDocType(file.name);
      // If batch type is manually locked, use it; otherwise use per-file guess
      const confirmed =
        manualUploadType && uploadDocType ? uploadDocType : guessed;
      return {
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        file,
        guessedType: guessed,
        confirmedType: confirmed,
        status: "pending" as PendingFileStatus,
      };
    });
    setPendingFiles((prev) => [...prev, ...entries]);
    // Switch to upload tab so user sees the queue
    setTab("upload");
  }

  function removeFromQueue(id: string) {
    setPendingFiles((prev) => prev.filter((f) => f.id !== id));
  }

  function setQueueFileType(id: string, type: string) {
    setPendingFiles((prev) =>
      prev.map((f) => (f.id === id ? { ...f, confirmedType: type } : f)),
    );
  }

  function clearDoneFromQueue() {
    setPendingFiles((prev) => prev.filter((f) => f.status === "pending"));
  }

  async function uploadPendingFiles() {
    const toUpload = pendingFiles.filter((f) => f.status === "pending");
    if (!toUpload.length) return;
    setUploading(true);
    setMessage(null);
    const uploadedIds: string[] = [];

    for (const entry of toUpload) {
      // Mark as uploading
      setPendingFiles((prev) =>
        prev.map((f) =>
          f.id === entry.id ? { ...f, status: "uploading" } : f,
        ),
      );

      // Effective type: batch manual override > per-file confirmed type
      const docType =
        manualUploadType && uploadDocType ? uploadDocType : entry.confirmedType;

      const params = new URLSearchParams({
        source_channel: sourceChannel || "upload",
        auto_process: String(autoProcess),
        auto_verify: String(autoVerify && autoProcess),
        manual_doc_type_override: String(Boolean(docType && manualUploadType)),
      });
      if (docType) params.set("requested_doc_type", docType);

      const form = new FormData();
      form.append("file", entry.file);
      const response = await fetch(`${API}/api/documents/ingest?${params}`, {
        method: "POST",
        body: form,
      }).catch(() => null);

      if (!response) {
        setPendingFiles((prev) =>
          prev.map((f) =>
            f.id === entry.id
              ? { ...f, status: "error", detail: "backend недоступен" }
              : f,
          ),
        );
        continue;
      }

      const payload = await response.json().catch(() => ({}));

      if (response.status === 202 || payload.quarantined) {
        setPendingFiles((prev) =>
          prev.map((f) =>
            f.id === entry.id
              ? {
                  ...f,
                  status: "quarantined",
                  detail: payload.reason ?? "карантин",
                }
              : f,
          ),
        );
      } else if (response.ok && payload.is_duplicate) {
        setPendingFiles((prev) =>
          prev.map((f) =>
            f.id === entry.id
              ? {
                  ...f,
                  status: "duplicate",
                  detail: `дубликат ${String(payload.duplicate_of).slice(0, 8)}`,
                }
              : f,
          ),
        );
      } else if (response.ok) {
        if (payload.id) uploadedIds.push(payload.id);
        setPendingFiles((prev) =>
          prev.map((f) =>
            f.id === entry.id
              ? {
                  ...f,
                  status: "done",
                  detail: payload.pipeline_queued
                    ? "пайплайн запущен"
                    : "сохранён",
                  detectedType: payload.detected_type ?? undefined,
                  detectedTypeSource: payload.detected_type_source ?? undefined,
                  documentId: payload.id,
                }
              : f,
          ),
        );
      } else {
        setPendingFiles((prev) =>
          prev.map((f) =>
            f.id === entry.id
              ? {
                  ...f,
                  status: "error",
                  detail: payload.detail ?? `HTTP ${response.status}`,
                }
              : f,
          ),
        );
      }
    }

    setUploading(false);
    const hasErrors = pendingFiles.some((f) => f.status === "error");
    setMessage(hasErrors ? "Есть ошибки" : "Готово");
    await loadWorkspace();
    if (uploadedIds.length) {
      setSelectedIds(new Set(uploadedIds));
      setSelectedId(uploadedIds[uploadedIds.length - 1]);
      setTab(autoProcess ? "queue" : "registry");
    }
  }

  async function runAction(action: string, fn: () => Promise<unknown>) {
    setBusyAction(action);
    setMessage(null);
    try {
      await fn();
      setMessage("Команда выполнена");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Ошибка выполнения");
    } finally {
      setBusyAction(null);
      await refreshSelected();
    }
  }

  async function updateDocument(patch: Record<string, unknown>) {
    if (!selected) return;
    await runAction("save", () =>
      requestJson(`/api/documents/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      }),
    );
  }

  async function batchAction(
    action: string,
    path: string,
    body: Record<string, unknown> = {},
  ) {
    if (!selectedIdsArray.length) return;
    await runAction(action, () =>
      requestJson(`/api/documents/${path}`, {
        method: path === "bulk-delete" ? "DELETE" : "POST",
        body: JSON.stringify({ document_ids: selectedIdsArray, ...body }),
      }),
    );
  }

  async function deleteCurrentDocument() {
    if (!selected) return;
    if (!confirm("Удалить документ и все связанные записи из баз данных?"))
      return;
    const deletingId = selected.id;
    await runAction("delete", () =>
      requestJson(`/api/documents/${deletingId}`, { method: "DELETE" }),
    );
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.delete(deletingId);
      return next;
    });
    setSelectedId(null);
  }

  async function createLink() {
    if (!selected || !targetDocumentId) return;
    await runAction("link", () =>
      requestJson(`/api/documents/${selected.id}/links`, {
        method: "POST",
        body: JSON.stringify({
          linked_entity_type: "document",
          linked_entity_id: targetDocumentId,
          link_type: linkType || "related",
        }),
      }),
    );
    setTargetDocumentId("");
    setTargetQuery("");
  }

  async function deleteLink(linkId: string) {
    if (!selected) return;
    await runAction("unlink", () =>
      fetch(`${API}/api/documents/${selected.id}/links/${linkId}`, {
        method: "DELETE",
      }),
    );
  }

  function toggleSelection(id: string, checked: boolean) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function toggleAll(checked: boolean) {
    if (checked) {
      setSelectedIds(
        new Set((workspace?.items ?? []).map((i) => i.document.id)),
      );
    } else {
      setSelectedIds(new Set());
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-950/95 px-6 py-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold">Документы</h1>
            <div className="mt-1 flex flex-wrap gap-3 text-xs text-slate-500">
              <span>Всего: {workspace?.total ?? 0}</span>
              <span>
                На проверку: {workspace?.status_counts.needs_review ?? 0}
              </span>
              <span>Карантин: {workspace?.status_counts.suspicious ?? 0}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {selectedIds.size > 0 && (
              <span className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300">
                Выбрано: {selectedIds.size}
              </span>
            )}
            <button
              onClick={() => {
                setTab("upload");
                fileInputRef.current?.click();
              }}
              disabled={uploading}
              className="rounded-md bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {uploading ? "Загрузка…" : "Добавить файлы"}
            </button>
          </div>
        </div>
        <div className="mt-4 flex gap-1 overflow-x-auto">
          {TABS.map((item) => (
            <button
              key={item.key}
              onClick={() => setTab(item.key)}
              className={`shrink-0 rounded-md px-3 py-2 text-sm ${
                tab === item.key
                  ? "bg-slate-100 text-slate-950"
                  : "text-slate-400 hover:bg-slate-900 hover:text-slate-100"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </header>

      <div className="grid min-h-[calc(100vh-130px)] grid-cols-1 xl:grid-cols-[minmax(0,1fr)_420px]">
        <main className="min-w-0 border-r border-slate-800 p-6">
          <Toolbar
            status={status}
            docType={docType}
            search={search}
            sourceChannel={sourceChannel}
            onStatus={setStatus}
            onDocType={setDocType}
            onSearch={setSearch}
            onSourceChannel={setSourceChannel}
          />

          {message && (
            <div className="mt-4 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300">
              {message}
            </div>
          )}

          {tab === "upload" && (
            <UploadPanel
              dragging={dragging}
              uploading={uploading}
              uploadDocType={uploadDocType}
              autoProcess={autoProcess}
              autoVerify={autoVerify}
              manualUploadType={manualUploadType}
              pendingFiles={pendingFiles}
              fileInputRef={fileInputRef}
              onDrag={setDragging}
              onUploadDocType={setUploadDocType}
              onAutoProcess={setAutoProcess}
              onAutoVerify={setAutoVerify}
              onManualUploadType={setManualUploadType}
              onAddFiles={addFilesToQueue}
              onRemoveFile={removeFromQueue}
              onSetFileType={setQueueFileType}
              onUpload={uploadPendingFiles}
              onClearDone={clearDoneFromQueue}
            />
          )}

          {tab === "registry" && (
            <RegistryPanel
              items={workspace?.items ?? []}
              selectedId={selectedId}
              selectedIds={selectedIds}
              busyAction={busyAction}
              onSelect={setSelectedId}
              onToggle={toggleSelection}
              onToggleAll={toggleAll}
              onBatch={(path, body) => batchAction(path, path, body)}
            />
          )}

          {tab === "queue" && (
            <QueuePanel
              items={workspace?.items ?? []}
              selectedId={selectedId}
              selectedIds={selectedIds}
              busyAction={busyAction}
              onSelect={setSelectedId}
              onBatchProcess={() => batchAction("process", "batch/process")}
              onBatchClassify={() =>
                batchAction("classify", "batch/classify", { force: true })
              }
              onBatchEmbeddings={() =>
                batchAction("embeddings", "batch/embeddings-reindex")
              }
              onBatchMemory={() =>
                batchAction("memory", "batch/memory-rebuild", {
                  build_scope: "extended",
                })
              }
            />
          )}

          {tab === "graph" && (
            <GraphPanel
              selected={selected}
              summary={summary}
              dependencies={dependencies}
              dependencyQuery={dependencyQuery}
              linkType={linkType}
              targetQuery={targetQuery}
              targetDocumentId={targetDocumentId}
              targetSearchResults={targetSearchResults}
              busyAction={busyAction}
              onDependencyQuery={setDependencyQuery}
              onSearchDependencies={() => loadDependencies(selectedId)}
              onLinkType={setLinkType}
              onTargetQuery={setTargetQuery}
              onTargetDocumentId={setTargetDocumentId}
              onCreateLink={createLink}
              onDeleteLink={deleteLink}
              onRebuild={(scope) =>
                selected &&
                runAction("memory", () =>
                  requestJson(
                    `/api/documents/${selected.id}/memory/rebuild?build_scope=${scope}`,
                    { method: "POST" },
                  ),
                )
              }
            />
          )}

          {tab === "ntd" && (
            <NtdPanel
              selected={selected}
              pipeline={pipeline}
              busyAction={busyAction}
              onRun={() =>
                selected &&
                runAction("ntd", () =>
                  requestJson(`/api/documents/${selected.id}/ntd-check`, {
                    method: "POST",
                    body: JSON.stringify({
                      document_id: selected.id,
                      triggered_by: "manual",
                      actor: "user",
                    }),
                  }),
                )
              }
              onCreateSource={() =>
                selected &&
                runAction("ntd-source", () =>
                  requestJson("/api/ntd/documents/from-source", {
                    method: "POST",
                    body: JSON.stringify({
                      source_document_id: selected.id,
                      index_immediately: true,
                    }),
                  }),
                )
              }
            />
          )}
        </main>

        <DetailPanel
          selected={selected}
          pipeline={pipeline}
          busyAction={busyAction}
          onUpdate={updateDocument}
          onDelete={deleteCurrentDocument}
          onClassify={() =>
            selected &&
            runAction("classify", () =>
              requestJson(`/api/documents/${selected.id}/classify?force=true`, {
                method: "POST",
              }),
            )
          }
          onExtract={() =>
            selected &&
            runAction("extract", () =>
              requestJson(`/api/documents/${selected.id}/extract?force=true`, {
                method: "POST",
              }),
            )
          }
        />
      </div>
    </div>
  );
}

function Toolbar({
  status,
  docType,
  search,
  sourceChannel,
  onStatus,
  onDocType,
  onSearch,
  onSourceChannel,
}: {
  status: string;
  docType: string;
  search: string;
  sourceChannel: string;
  onStatus: (value: string) => void;
  onDocType: (value: string) => void;
  onSearch: (value: string) => void;
  onSourceChannel: (value: string) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-[1.1fr_0.8fr_0.8fr_0.8fr]">
      <input
        value={search}
        onChange={(event) => onSearch(event.target.value)}
        placeholder="Поиск по имени или hash"
        className="rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-blue-500"
      />
      <select
        value={status}
        onChange={(event) => onStatus(event.target.value)}
        className="rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm"
      >
        {STATUS_FILTERS.map((item) => (
          <option key={item.value} value={item.value}>
            {item.label}
          </option>
        ))}
      </select>
      <select
        value={docType}
        onChange={(event) => onDocType(event.target.value)}
        className="rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm"
      >
        <option value="">Все типы</option>
        {DOC_TYPES.filter((item) => item.value).map((item) => (
          <option key={item.value} value={item.value}>
            {item.label}
          </option>
        ))}
      </select>
      <input
        value={sourceChannel}
        onChange={(event) => onSourceChannel(event.target.value)}
        placeholder="Источник"
        className="rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-blue-500"
      />
    </div>
  );
}

function typeSourceBadge(source: string | undefined, guessedType: string) {
  if (!source && !guessedType)
    return (
      <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-400">
        авто
      </span>
    );
  if (source === "manual")
    return (
      <span className="rounded bg-green-900 px-1.5 py-0.5 text-xs text-green-300">
        вручную
      </span>
    );
  if (source === "extension" || (!source && guessedType))
    return (
      <span className="rounded bg-blue-900 px-1.5 py-0.5 text-xs text-blue-300">
        ↑ расширение
      </span>
    );
  if (source === "ai")
    return (
      <span className="rounded bg-violet-900 px-1.5 py-0.5 text-xs text-violet-300">
        ИИ
      </span>
    );
  return (
    <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-400">
      {TYPE_SOURCE_LABEL[source ?? ""] ?? source}
    </span>
  );
}

function pendingFileStatusIcon(status: PendingFileStatus) {
  if (status === "uploading")
    return (
      <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
    );
  if (status === "done") return <span className="text-emerald-400">✓</span>;
  if (status === "error") return <span className="text-red-400">✗</span>;
  if (status === "quarantined")
    return <span className="text-amber-400">⚠</span>;
  if (status === "duplicate") return <span className="text-slate-400">≡</span>;
  return null;
}

function UploadPanel({
  dragging,
  uploading,
  uploadDocType,
  autoProcess,
  autoVerify,
  manualUploadType,
  pendingFiles,
  fileInputRef,
  onDrag,
  onUploadDocType,
  onAutoProcess,
  onAutoVerify,
  onManualUploadType,
  onAddFiles,
  onRemoveFile,
  onSetFileType,
  onUpload,
  onClearDone,
}: {
  dragging: boolean;
  uploading: boolean;
  uploadDocType: string;
  autoProcess: boolean;
  autoVerify: boolean;
  manualUploadType: boolean;
  pendingFiles: PendingFile[];
  fileInputRef: RefObject<HTMLInputElement | null>;
  onDrag: (value: boolean) => void;
  onUploadDocType: (value: string) => void;
  onAutoProcess: (value: boolean) => void;
  onAutoVerify: (value: boolean) => void;
  onManualUploadType: (value: boolean) => void;
  onAddFiles: (files: FileList | File[]) => void;
  onRemoveFile: (id: string) => void;
  onSetFileType: (id: string, type: string) => void;
  onUpload: () => void;
  onClearDone: () => void;
}) {
  const pendingCount = pendingFiles.filter(
    (f) => f.status === "pending",
  ).length;
  const doneCount = pendingFiles.filter(
    (f) =>
      f.status === "done" ||
      f.status === "duplicate" ||
      f.status === "quarantined",
  ).length;

  return (
    <section className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".pdf,.dwg,.dxf,.svg,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp,.gif,.docx,.xlsx,.xls,.csv,.json,.xml,.txt,.step,.stp,.iges,.igs"
        className="hidden"
        onChange={(event) => {
          if (event.target.files) onAddFiles(event.target.files);
          // Reset so same file can be re-selected
          event.target.value = "";
        }}
      />

      <div className="flex flex-col gap-3">
        {/* Drop zone — compact when files are queued */}
        <div
          onDragOver={(event) => {
            event.preventDefault();
            onDrag(true);
          }}
          onDragLeave={() => onDrag(false)}
          onDrop={(event) => {
            event.preventDefault();
            onDrag(false);
            onAddFiles(event.dataTransfer.files);
          }}
          onClick={() => fileInputRef.current?.click()}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-md border border-dashed px-6 text-center transition ${
            pendingFiles.length > 0
              ? "min-h-[80px] py-4"
              : "min-h-[260px] py-10"
          } ${
            dragging
              ? "border-blue-400 bg-blue-950/40"
              : "border-slate-700 bg-slate-900 hover:border-slate-500"
          }`}
        >
          <div className="text-sm font-semibold text-slate-300">
            {uploading ? "Идёт загрузка…" : "Перетащите или выберите файлы"}
          </div>
          {pendingFiles.length === 0 && (
            <div className="mt-1.5 max-w-lg text-xs text-slate-500">
              PDF, DWG, DXF, SVG, PNG/JPG, DOCX, XLSX, TXT, STEP, XML, CSV
              <br />
              <span className="text-blue-500">
                DWG/DXF → автоматически запускается анализ чертежа
              </span>
            </div>
          )}
        </div>

        {/* File queue */}
        {pendingFiles.length > 0 && (
          <div className="overflow-hidden rounded-md border border-slate-800">
            <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900 px-3 py-2">
              <span className="text-xs text-slate-400">
                Очередь: {pendingCount} ожидает
                {doneCount > 0 && `, ${doneCount} загружено`}
              </span>
              {doneCount > 0 && (
                <button
                  onClick={onClearDone}
                  className="text-xs text-slate-500 hover:text-slate-300"
                >
                  Очистить загруженные
                </button>
              )}
            </div>
            <div className="max-h-[340px] divide-y divide-slate-800 overflow-y-auto">
              {pendingFiles.map((pf) => {
                const typeLabel =
                  DOC_TYPES.find((t) => t.value === pf.confirmedType)?.label ??
                  "Авто";
                const detectedLabel = pf.detectedType
                  ? (DOC_TYPES.find((t) => t.value === pf.detectedType)
                      ?.label ?? pf.detectedType)
                  : null;

                return (
                  <div
                    key={pf.id}
                    className="flex items-start gap-2 px-3 py-2.5"
                  >
                    {/* Status icon */}
                    <div className="mt-0.5 w-4 shrink-0 text-center text-sm">
                      {pendingFileStatusIcon(pf.status)}
                    </div>

                    {/* File info */}
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="truncate text-xs font-medium text-slate-200">
                          {pf.file.name}
                        </span>
                        <span className="shrink-0 text-xs text-slate-600">
                          {fmtBytes(pf.file.size)}
                        </span>
                      </div>

                      {/* Type row */}
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        {pf.status === "pending" ||
                        pf.status === "uploading" ? (
                          <>
                            {/* Editable type selector */}
                            <select
                              value={pf.confirmedType}
                              disabled={
                                pf.status === "uploading" ||
                                (manualUploadType && !!uploadDocType)
                              }
                              onChange={(e) =>
                                onSetFileType(pf.id, e.target.value)
                              }
                              className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-xs text-slate-200 disabled:opacity-50"
                            >
                              {DOC_TYPES.map((t) => (
                                <option key={t.value} value={t.value}>
                                  {t.label}
                                </option>
                              ))}
                            </select>
                            {/* Source badge — shown only when guessed from extension */}
                            {pf.guessedType &&
                              pf.guessedType === pf.confirmedType &&
                              typeSourceBadge("extension", pf.guessedType)}
                          </>
                        ) : (
                          <>
                            {/* After upload: show confirmed + detected */}
                            <span className="text-xs text-slate-400">
                              {typeLabel}
                            </span>
                            {detectedLabel && detectedLabel !== typeLabel && (
                              <>
                                <span className="text-xs text-slate-600">
                                  →
                                </span>
                                <span className="text-xs text-emerald-400">
                                  {detectedLabel}
                                </span>
                                {typeSourceBadge(
                                  pf.detectedTypeSource,
                                  pf.detectedType ?? "",
                                )}
                              </>
                            )}
                            {detectedLabel &&
                              detectedLabel === typeLabel &&
                              typeSourceBadge(
                                pf.detectedTypeSource,
                                pf.detectedType ?? "",
                              )}
                          </>
                        )}
                      </div>

                      {/* Result detail */}
                      {pf.detail && (
                        <div
                          className={`mt-0.5 text-xs ${
                            pf.status === "error"
                              ? "text-red-400"
                              : pf.status === "quarantined"
                                ? "text-amber-400"
                                : pf.status === "duplicate"
                                  ? "text-slate-500"
                                  : "text-emerald-400"
                          }`}
                        >
                          {pf.detail}
                        </div>
                      )}
                    </div>

                    {/* Remove button (only for pending) */}
                    {pf.status === "pending" && (
                      <button
                        onClick={() => onRemoveFile(pf.id)}
                        className="ml-1 shrink-0 text-slate-600 hover:text-red-400"
                        title="Убрать из очереди"
                      >
                        ×
                      </button>
                    )}
                  </div>
                );
              })}
            </div>

            {/* Upload button */}
            {pendingCount > 0 && (
              <div className="border-t border-slate-800 bg-slate-950 px-3 py-2">
                <button
                  onClick={onUpload}
                  disabled={uploading}
                  className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 transition-colors"
                >
                  {uploading
                    ? "Загружается…"
                    : `Загрузить ${pendingCount} ${pendingCount === 1 ? "файл" : pendingCount < 5 ? "файла" : "файлов"}`}
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Settings panel */}
      <div className="rounded-md border border-slate-800 bg-slate-900 p-4">
        <h2 className="text-sm font-semibold">Параметры партии</h2>
        <label className="mt-4 block text-xs text-slate-400">
          Тип документа (для всей партии)
          <select
            value={uploadDocType}
            onChange={(event) => onUploadDocType(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
          >
            {DOC_TYPES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        {uploadDocType && (
          <p className="mt-1 text-xs text-slate-500">
            Тип применяется ко всей партии. Для каждого файла можно задать свой
            тип в очереди выше.
          </p>
        )}
        <label className="mt-4 flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={manualUploadType}
            disabled={!uploadDocType}
            onChange={(event) => onManualUploadType(event.target.checked)}
            suppressHydrationWarning
          />
          <span>
            Закрепить тип (игнорировать авто)
            <span className="block text-xs text-slate-500 mt-0.5">
              Если включено — тип партии перекрывает авто-определение по файлу
            </span>
          </span>
        </label>
        <label className="mt-3 flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={autoProcess}
            onChange={(event) => onAutoProcess(event.target.checked)}
            suppressHydrationWarning
          />
          Запускать полный пайплайн
        </label>
        <label
          className={`mt-3 flex items-start gap-2 text-sm ${
            autoProcess ? "text-slate-300" : "cursor-not-allowed text-slate-600"
          }`}
        >
          <input
            type="checkbox"
            checked={autoVerify}
            disabled={!autoProcess}
            onChange={(event) => onAutoVerify(event.target.checked)}
            className="mt-0.5"
            suppressHydrationWarning
          />
          <span>
            Автоматически проверять и утверждать
            <span className="mt-0.5 block text-xs text-slate-500">
              Повторное извлечение проверочными моделями, утверждение при 95%+
              консенсусе
            </span>
          </span>
        </label>

        {/* Legend */}
        <div className="mt-5 border-t border-slate-800 pt-4">
          <p className="text-xs font-medium text-slate-400">Источник типа</p>
          <div className="mt-2 flex flex-col gap-1.5">
            {typeSourceBadge("extension", "drawing")}
            <span className="text-xs text-slate-500">
              — определён по расширению файла
            </span>
            {typeSourceBadge("ai", "")}
            <span className="text-xs text-slate-500">
              — определён ИИ после анализа
            </span>
            {typeSourceBadge("manual", "")}
            <span className="text-xs text-slate-500">— задан вручную</span>
          </div>
        </div>
      </div>
    </section>
  );
}

function RegistryPanel({
  items,
  selectedId,
  selectedIds,
  busyAction,
  onSelect,
  onToggle,
  onToggleAll,
  onBatch,
}: {
  items: WorkspaceItem[];
  selectedId: string | null;
  selectedIds: Set<string>;
  busyAction: string | null;
  onSelect: (id: string) => void;
  onToggle: (id: string, checked: boolean) => void;
  onToggleAll: (checked: boolean) => void;
  onBatch: (path: string, body?: Record<string, unknown>) => void;
}) {
  return (
    <section className="mt-5 overflow-hidden rounded-md border border-slate-800">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-slate-900 px-3 py-2">
        <span className="text-sm text-slate-300">
          Документы: {items.length}
        </span>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => onBatch("batch/process")}
            disabled={!selectedIds.size || Boolean(busyAction)}
            className="rounded-md bg-slate-700 px-3 py-1.5 text-xs hover:bg-slate-600 disabled:opacity-50"
          >
            Обработать
          </button>
          <button
            onClick={() => onBatch("bulk-delete", { delete_files: true })}
            disabled={!selectedIds.size || Boolean(busyAction)}
            className="rounded-md bg-red-950 px-3 py-1.5 text-xs text-red-200 hover:bg-red-900 disabled:opacity-50"
          >
            Удалить
          </button>
        </div>
      </div>
      <DocumentTable
        items={items}
        selectedId={selectedId}
        selectedIds={selectedIds}
        onSelect={onSelect}
        onToggle={onToggle}
        onToggleAll={onToggleAll}
      />
    </section>
  );
}

function QueuePanel({
  items,
  selectedId,
  selectedIds,
  busyAction,
  onSelect,
  onBatchProcess,
  onBatchClassify,
  onBatchEmbeddings,
  onBatchMemory,
}: {
  items: WorkspaceItem[];
  selectedId: string | null;
  selectedIds: Set<string>;
  busyAction: string | null;
  onSelect: (id: string) => void;
  onBatchProcess: () => void;
  onBatchClassify: () => void;
  onBatchEmbeddings: () => void;
  onBatchMemory: () => void;
}) {
  return (
    <section className="mt-5 space-y-4">
      <div className="flex flex-wrap gap-2">
        {[
          { label: "Полный пайплайн", handler: onBatchProcess },
          { label: "Классификация", handler: onBatchClassify },
          { label: "Память и граф", handler: onBatchMemory },
          { label: "Векторизация", handler: onBatchEmbeddings },
        ].map((item) => (
          <button
            key={item.label}
            onClick={item.handler}
            disabled={!selectedIds.size || Boolean(busyAction)}
            className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600 disabled:opacity-50"
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="overflow-hidden rounded-md border border-slate-800">
        <div className="grid grid-cols-[minmax(220px,1.1fr)_minmax(360px,2fr)_90px] border-b border-slate-800 bg-slate-900 px-3 py-2 text-xs text-slate-500">
          <span>Документ</span>
          <span>Этапы</span>
          <span>Прогресс</span>
        </div>
        {items.map((item) => (
          <button
            key={item.document.id}
            onClick={() => onSelect(item.document.id)}
            className={`grid w-full grid-cols-[minmax(220px,1.1fr)_minmax(360px,2fr)_90px] items-center gap-3 border-b border-slate-900 px-3 py-3 text-left text-sm hover:bg-slate-900 ${
              selectedId === item.document.id ? "bg-slate-900" : ""
            }`}
          >
            <span className="min-w-0">
              <span className="block truncate font-medium text-slate-100">
                {item.document.file_name}
              </span>
              <span className="text-xs text-slate-500">
                {statusLabel(item.document.status)}
              </span>
              {item.pipeline.processing_error && (
                <span className="mt-1 block truncate text-xs text-red-300">
                  {item.pipeline.processing_error}
                </span>
              )}
            </span>
            <PipelineSteps steps={pipelineSteps(item.pipeline)} compact />
            <ProgressBar
              value={pipelineProgress(item.pipeline)}
              failed={Boolean(item.pipeline.processing_error)}
            />
          </button>
        ))}
        {!items.length && (
          <div className="p-8 text-center text-sm text-slate-500">
            Нет данных
          </div>
        )}
      </div>
    </section>
  );
}

function DocumentTable({
  items,
  selectedId,
  selectedIds,
  onSelect,
  onToggle,
  onToggleAll,
}: {
  items: WorkspaceItem[];
  selectedId: string | null;
  selectedIds: Set<string>;
  onSelect: (id: string) => void;
  onToggle: (id: string, checked: boolean) => void;
  onToggleAll: (checked: boolean) => void;
}) {
  const allChecked = items.length > 0 && selectedIds.size === items.length;
  const indeterminate = selectedIds.size > 0 && selectedIds.size < items.length;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[880px] border-collapse text-sm">
        <thead className="bg-slate-900 text-xs text-slate-500">
          <tr>
            <th className="w-10 px-3 py-2 text-left">
              <input
                type="checkbox"
                checked={allChecked}
                ref={(el) => {
                  if (el) el.indeterminate = indeterminate;
                }}
                onChange={(e) => onToggleAll(e.target.checked)}
                disabled={items.length === 0}
                title="Выбрать все"
              />
            </th>
            <th className="px-3 py-2 text-left">Файл</th>
            <th className="px-3 py-2 text-left">Тип</th>
            <th className="px-3 py-2 text-left">Статус</th>
            <th className="px-3 py-2 text-left">Память</th>
            <th className="px-3 py-2 text-left">Граф</th>
            <th className="px-3 py-2 text-left">Дата</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr
              key={item.document.id}
              onClick={() => onSelect(item.document.id)}
              className={`cursor-pointer border-t border-slate-900 hover:bg-slate-900 ${
                selectedId === item.document.id ? "bg-slate-900" : ""
              }`}
            >
              <td
                className="px-3 py-3"
                onClick={(event) => event.stopPropagation()}
              >
                <input
                  type="checkbox"
                  checked={selectedIds.has(item.document.id)}
                  onChange={(event) =>
                    onToggle(item.document.id, event.target.checked)
                  }
                />
              </td>
              <td className="max-w-[360px] px-3 py-3">
                <div className="truncate font-medium">
                  {item.document.file_name}
                </div>
                <div className="text-xs text-slate-500">
                  {fmtBytes(item.document.file_size)} ·{" "}
                  {item.document.source_channel ?? "upload"}
                </div>
              </td>
              <td className="px-3 py-3">
                {docTypeLabel(item.document.doc_type)}
              </td>
              <td className="px-3 py-3">{statusLabel(item.document.status)}</td>
              <td className="px-3 py-3">{item.pipeline.memory_chunks}</td>
              <td className="px-3 py-3">
                {item.pipeline.graph_nodes}/{item.pipeline.graph_edges}
              </td>
              <td className="px-3 py-3 text-slate-400">
                {new Date(item.document.created_at).toLocaleDateString("ru-RU")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!items.length && (
        <div className="p-8 text-center text-sm text-slate-500">
          Нет документов
        </div>
      )}
    </div>
  );
}

function GraphPanel({
  selected,
  summary,
  dependencies,
  dependencyQuery,
  linkType,
  targetQuery,
  targetDocumentId,
  targetSearchResults,
  busyAction,
  onDependencyQuery,
  onSearchDependencies,
  onLinkType,
  onTargetQuery,
  onTargetDocumentId,
  onCreateLink,
  onDeleteLink,
  onRebuild,
}: {
  selected: DocumentItem | null;
  summary: ManagementSummary | null;
  dependencies: DependenciesSummary | null;
  dependencyQuery: string;
  linkType: string;
  targetQuery: string;
  targetDocumentId: string;
  targetSearchResults: SearchDocument[];
  busyAction: string | null;
  onDependencyQuery: (value: string) => void;
  onSearchDependencies: () => void;
  onLinkType: (value: string) => void;
  onTargetQuery: (value: string) => void;
  onTargetDocumentId: (value: string) => void;
  onCreateLink: () => void;
  onDeleteLink: (id: string) => void;
  onRebuild: (scope: string) => void;
}) {
  if (!selected) return <EmptySelection />;
  return (
    <section className="mt-5 space-y-5">
      <div className="grid gap-4 md:grid-cols-3">
        <Metric label="Явные связи" value={summary?.links.length ?? 0} />
        <Metric label="Узлы графа" value={dependencies?.total_nodes ?? 0} />
        <Metric label="Ребра графа" value={dependencies?.total_edges ?? 0} />
      </div>
      <div className="rounded-md border border-slate-800 bg-slate-900 p-4">
        <div className="grid gap-2 md:grid-cols-[1fr_auto_auto]">
          <input
            value={dependencyQuery}
            onChange={(event) => onDependencyQuery(event.target.value)}
            placeholder="Поиск по зависимостям"
            className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <button
            onClick={onSearchDependencies}
            className="rounded-md bg-slate-700 px-3 py-2 text-sm"
          >
            Найти
          </button>
          <div className="flex gap-2">
            <button
              onClick={() => onRebuild("compact")}
              disabled={Boolean(busyAction)}
              className="rounded-md bg-slate-700 px-3 py-2 text-sm disabled:opacity-50"
            >
              Compact
            </button>
            <button
              onClick={() => onRebuild("extended")}
              disabled={Boolean(busyAction)}
              className="rounded-md bg-slate-700 px-3 py-2 text-sm disabled:opacity-50"
            >
              Extended
            </button>
          </div>
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-[0.7fr_1fr_1fr_auto]">
          <input
            value={linkType}
            onChange={(event) => onLinkType(event.target.value)}
            placeholder="Тип связи"
            className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <input
            value={targetQuery}
            onChange={(event) => onTargetQuery(event.target.value)}
            placeholder="Найти документ для связи"
            className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <select
            value={targetDocumentId}
            onChange={(event) => onTargetDocumentId(event.target.value)}
            className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          >
            <option value="">Не выбран</option>
            {targetSearchResults
              .filter((item) => item.id !== selected.id)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.file_name}
                </option>
              ))}
          </select>
          <button
            onClick={onCreateLink}
            disabled={!targetDocumentId || Boolean(busyAction)}
            className="rounded-md bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
          >
            Добавить
          </button>
        </div>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <ListPanel
          title="Явные связи"
          items={(summary?.links ?? []).map((link) => ({
            id: link.id,
            title: `${link.link_type}: ${link.linked_entity_type}`,
            detail: link.linked_entity_id,
            action: () => onDeleteLink(link.id),
          }))}
        />
        <ListPanel
          title="Узлы памяти"
          items={(dependencies?.nodes ?? []).map((node) => ({
            id: node.id,
            title: `${node.node_type}: ${node.title}`,
            detail: node.summary ?? `confidence ${node.confidence.toFixed(2)}`,
          }))}
        />
        <ListPanel
          title="Ребра графа"
          items={(dependencies?.edges ?? []).map((edge) => ({
            id: edge.id,
            title: edge.edge_type,
            detail:
              edge.reason ??
              `${edge.source_node_id.slice(0, 8)} -> ${edge.target_node_id.slice(0, 8)}`,
          }))}
        />
      </div>
    </section>
  );
}

function NtdPanel({
  selected,
  pipeline,
  busyAction,
  onRun,
  onCreateSource,
}: {
  selected: DocumentItem | null;
  pipeline: PipelineStatus | null;
  busyAction: string | null;
  onRun: () => void;
  onCreateSource: () => void;
}) {
  if (!selected) return <EmptySelection />;
  return (
    <section className="mt-5 space-y-5">
      <div className="grid gap-4 md:grid-cols-3">
        <Metric label="Проверок" value={pipeline?.ntd_checks ?? 0} />
        <Metric
          label="Открытых замечаний"
          value={pipeline?.ntd_open_findings ?? 0}
        />
        <Metric label="Evidence spans" value={pipeline?.evidence_spans ?? 0} />
      </div>
      <div className="flex flex-wrap gap-2 rounded-md border border-slate-800 bg-slate-900 p-4">
        <button
          onClick={onRun}
          disabled={Boolean(busyAction)}
          className="rounded-md bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
        >
          Проверить на соответствие НТД
        </button>
        <button
          onClick={onCreateSource}
          disabled={Boolean(busyAction)}
          className="rounded-md bg-slate-700 px-3 py-2 text-sm disabled:opacity-50"
        >
          Занести как НТД
        </button>
        <Link
          href="/settings/ntd"
          className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600"
        >
          База НТД
        </Link>
      </div>
    </section>
  );
}

function DetailPanel({
  selected,
  pipeline,
  busyAction,
  onUpdate,
  onDelete,
  onClassify,
  onExtract,
}: {
  selected: DocumentItem | null;
  pipeline: PipelineStatus | null;
  busyAction: string | null;
  onUpdate: (patch: Record<string, unknown>) => void;
  onDelete: () => void;
  onClassify: () => void;
  onExtract: () => void;
}) {
  return (
    <aside className="min-w-0 bg-slate-950 p-5">
      {!selected ? (
        <EmptySelection />
      ) : (
        <div className="space-y-5">
          <div>
            <h2 className="line-clamp-2 text-lg font-semibold">
              {selected.file_name}
            </h2>
            <p className="mt-1 text-xs text-slate-500">
              {selected.mime_type} · {fmtBytes(selected.file_size)}
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Metric
              label="Извлечений"
              value={pipeline?.extraction_count ?? 0}
              compact
            />
            <Metric label="Граф" value={pipeline?.graph_nodes ?? 0} compact />
            <Metric
              label="Векторов"
              value={pipeline?.embedding_records ?? 0}
              compact
            />
            <Metric
              label="НТД"
              value={pipeline?.ntd_open_findings ?? 0}
              compact
            />
          </div>
          <PipelineProgressCard pipeline={pipeline} />
          <label
            key={`${selected.id}-file-name`}
            className="block text-xs text-slate-400"
          >
            Имя файла
            <input
              defaultValue={selected.file_name}
              onBlur={(event) => {
                const nextValue = event.target.value.trim();
                if (nextValue && nextValue !== selected.file_name) {
                  onUpdate({ file_name: nextValue });
                }
              }}
              className="mt-1 w-full rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-100"
            />
          </label>
          <label className="block text-xs text-slate-400">
            Тип документа
            <select
              value={selected.doc_type ?? ""}
              onChange={(event) =>
                onUpdate({
                  doc_type: event.target.value || null,
                  manual_doc_type_override: Boolean(event.target.value),
                })
              }
              className="mt-1 w-full rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-100"
            >
              {DOC_TYPES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs text-slate-400">
            Статус
            <select
              value={selected.status}
              onChange={(event) => onUpdate({ status: event.target.value })}
              className="mt-1 w-full rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-100"
            >
              {STATUS_FILTERS.filter((item) => item.value).map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label
            key={`${selected.id}-source`}
            className="block text-xs text-slate-400"
          >
            Источник
            <input
              defaultValue={selected.source_channel ?? ""}
              onBlur={(event) => {
                const nextValue = event.target.value.trim() || null;
                if (nextValue !== selected.source_channel) {
                  onUpdate({ source_channel: nextValue });
                }
              }}
              className="mt-1 w-full rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-100"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={onClassify}
              disabled={Boolean(busyAction)}
              className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600 disabled:opacity-50"
            >
              Классифицировать
            </button>
            <button
              onClick={onExtract}
              disabled={Boolean(busyAction)}
              className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600 disabled:opacity-50"
            >
              Распознать
            </button>
            <Link
              href={`/documents/${selected.id}/review`}
              className="rounded-md bg-slate-700 px-3 py-2 text-center text-sm hover:bg-slate-600"
            >
              Review
            </Link>
            <a
              href={`${API}/api/documents/${selected.id}/download`}
              className="rounded-md bg-slate-700 px-3 py-2 text-center text-sm hover:bg-slate-600"
            >
              Скачать
            </a>
          </div>
          <button
            onClick={onDelete}
            disabled={Boolean(busyAction)}
            className="w-full rounded-md bg-red-950 px-3 py-2 text-sm text-red-200 hover:bg-red-900 disabled:opacity-50"
          >
            Удалить полностью
          </button>
          {pipeline?.processing_error && (
            <div className="rounded-md border border-red-900 bg-red-950/30 p-3 text-sm text-red-200">
              {pipeline.processing_error}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function ProgressBar({
  value,
  failed = false,
}: {
  value: number;
  failed?: boolean;
}) {
  return (
    <div>
      <div className="mb-1 text-xs text-slate-500">{value}%</div>
      <div className="h-2 overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full ${failed ? "bg-red-500" : "bg-emerald-500"}`}
          style={{ width: `${Math.max(4, value)}%` }}
        />
      </div>
    </div>
  );
}

function PipelineSteps({
  steps,
  compact = false,
}: {
  steps: PipelineStep[];
  compact?: boolean;
}) {
  const colors: Record<string, string> = {
    done: "border-emerald-900 bg-emerald-950/50 text-emerald-200",
    skipped: "border-slate-700 bg-slate-900 text-slate-500",
    running: "border-blue-800 bg-blue-950/60 text-blue-200",
    queued: "border-amber-800 bg-amber-950/40 text-amber-200",
    failed: "border-red-800 bg-red-950/50 text-red-200",
    pending: "border-slate-800 bg-slate-950 text-slate-600",
  };
  return (
    <div className={`flex flex-wrap ${compact ? "gap-1" : "gap-2"}`}>
      {steps.map((step) => (
        <span
          key={step.key}
          title={step.error ?? step.status}
          className={`rounded-md border px-2 py-1 text-[11px] ${
            colors[step.status] ?? colors.pending
          }`}
        >
          {step.label}
        </span>
      ))}
    </div>
  );
}

function PipelineProgressCard({
  pipeline,
}: {
  pipeline: PipelineStatus | null;
}) {
  const progress = pipelineProgress(pipeline);
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">Пайплайн</p>
          <p className="mt-1 text-sm text-slate-300">
            {pipeline?.processing_status ?? "нет задачи"}
            {pipeline?.current_step ? ` · ${pipeline.current_step}` : ""}
          </p>
        </div>
        <div className="w-24">
          <ProgressBar
            value={progress}
            failed={Boolean(pipeline?.processing_error)}
          />
        </div>
      </div>
      <PipelineSteps steps={pipelineSteps(pipeline)} />
    </div>
  );
}

function Metric({
  label,
  value,
  compact = false,
}: {
  label: string;
  value: number;
  compact?: boolean;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p
        className={
          compact ? "mt-1 text-lg font-semibold" : "mt-1 text-2xl font-semibold"
        }
      >
        {value}
      </p>
    </div>
  );
}

function ListPanel({
  title,
  items,
}: {
  title: string;
  items: Array<{
    id: string;
    title: string;
    detail: string;
    action?: () => void;
  }>;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-3">
      <h3 className="text-xs font-semibold uppercase text-slate-500">
        {title}
      </h3>
      <div className="mt-2 max-h-96 overflow-y-auto divide-y divide-slate-800">
        {items.map((item) => (
          <div key={item.id} className="flex gap-2 py-2">
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-slate-100">
                {item.title}
              </div>
              <div className="mt-1 line-clamp-2 text-xs text-slate-500">
                {item.detail}
              </div>
            </div>
            {item.action && (
              <button
                onClick={item.action}
                className="self-start rounded-md bg-red-950 px-2 py-1 text-xs text-red-200"
              >
                Удалить
              </button>
            )}
          </div>
        ))}
        {!items.length && (
          <div className="py-6 text-sm text-slate-600">Нет данных</div>
        )}
      </div>
    </div>
  );
}

function EmptySelection() {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-8 text-center text-sm text-slate-500">
      Выберите документ
    </div>
  );
}
