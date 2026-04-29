"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

interface DocumentItem {
  id: string;
  file_name: string;
  file_hash: string;
  file_size: number;
  mime_type: string;
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

interface ManagementSummary {
  document: DocumentItem;
  pipeline: PipelineStatus;
  links: Array<{
    id: string;
    linked_entity_type: string;
    linked_entity_id: string;
    link_type: string;
  }>;
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
  status: "uploaded" | "quarantined" | "failed";
  detail: string;
}

const STATUS_FILTERS = [
  { value: "", label: "Все" },
  { value: "ingested", label: "Загружены" },
  { value: "extracting", label: "Распознаются" },
  { value: "needs_review", label: "На проверку" },
  { value: "approved", label: "Утверждены" },
  { value: "suspicious", label: "Карантин" },
  { value: "archived", label: "Архив" },
];

const DOC_TYPES = [
  { value: "", label: "Не задан" },
  { value: "invoice", label: "Счет" },
  { value: "letter", label: "Письмо" },
  { value: "contract", label: "Договор" },
  { value: "drawing", label: "Чертеж" },
  { value: "commercial_offer", label: "КП" },
  { value: "act", label: "Акт" },
  { value: "waybill", label: "Накладная" },
  { value: "other", label: "Другое" },
];

function fmtBytes(value: number) {
  if (value > 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value > 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

export default function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [summary, setSummary] = useState<ManagementSummary | null>(null);
  const [status, setStatus] = useState("");
  const [docType, setDocType] = useState("");
  const [search, setSearch] = useState("");
  const [sourceChannel, setSourceChannel] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadResults, setUploadResults] = useState<UploadResult[]>([]);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dependencyQuery, setDependencyQuery] = useState("");
  const [dependencies, setDependencies] = useState<DependenciesSummary | null>(null);
  const [linkType, setLinkType] = useState("related");
  const [linkedEntityType, setLinkedEntityType] = useState("document");
  const [linkedEntityId, setLinkedEntityId] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadDocuments = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    if (docType) params.set("doc_type", docType);
    if (sourceChannel) params.set("source_channel", sourceChannel);
    if (search.trim()) params.set("search", search.trim());
    const response = await fetch(`${API}/api/documents?${params}`).catch(() => null);
    if (!response?.ok) {
      setDocuments([]);
      return;
    }
    const data = await response.json();
    const items = data.items ?? [];
    setDocuments(items);
    setSelectedId((current) => current ?? items[0]?.id ?? null);
  }, [docType, search, sourceChannel, status]);

  const loadSummary = useCallback(async (id: string | null) => {
    if (!id) {
      setSummary(null);
      return;
    }
    const response = await fetch(`${API}/api/documents/${id}/management`).catch(
      () => null,
    );
    if (!response?.ok) {
      setSummary(null);
      return;
    }
    setSummary(await response.json());
  }, []);

  const loadDependencies = useCallback(
    async (id: string | null) => {
      if (!id) {
        setDependencies(null);
        return;
      }
      const params = new URLSearchParams({ depth: "2", limit: "150" });
      if (dependencyQuery.trim()) params.set("query", dependencyQuery.trim());
      const response = await fetch(
        `${API}/api/documents/${id}/dependencies?${params}`,
      ).catch(() => null);
      if (!response?.ok) {
        setDependencies(null);
        return;
      }
      setDependencies(await response.json());
    },
    [dependencyQuery],
  );

  useEffect(() => {
    loadDocuments();
  }, [loadDocuments]);

  useEffect(() => {
    loadSummary(selectedId);
  }, [loadSummary, selectedId]);

  useEffect(() => {
    loadDependencies(selectedId);
  }, [loadDependencies, selectedId]);

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setMessage(null);
    const results: UploadResult[] = [];
    for (const file of Array.from(files)) {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(
        `${API}/api/documents/ingest?source_channel=${encodeURIComponent(sourceChannel || "upload")}`,
        { method: "POST", body: form },
      ).catch(() => null);
      if (!response) {
        results.push({
          fileName: file.name,
          status: "failed",
          detail: "backend недоступен",
        });
        continue;
      }
      const payload = await response.json().catch(() => ({}));
      if (response.status === 202 || payload.quarantined) {
        results.push({
          fileName: file.name,
          status: "quarantined",
          detail: payload.reason ?? "файл отправлен в карантин",
        });
      } else if (response.ok) {
        results.push({
          fileName: file.name,
          status: "uploaded",
          detail: "загружен, обработка поставлена в очередь",
        });
      } else {
        results.push({
          fileName: file.name,
          status: "failed",
          detail: payload.detail ?? `HTTP ${response.status}`,
        });
      }
    }
    setUploading(false);
    setUploadResults(results);
    setMessage(
      results.some((item) => item.status === "failed")
        ? "Часть файлов не загрузилась"
        : "Загрузка завершена",
    );
    await loadDocuments();
  }

  async function runAction(action: string, fn: () => Promise<Response | void>) {
    if (!selectedId) return;
    setBusyAction(action);
    setMessage(null);
    const response = await fn().catch(() => null);
    if (response && "ok" in response && !response.ok) {
      setMessage(`Ошибка выполнения: ${action}`);
    } else {
      setMessage("Команда выполнена");
    }
    setBusyAction(null);
    await loadDocuments();
    await loadSummary(selectedId);
  }

  async function updateDocument(patch: Record<string, unknown>) {
    if (!selectedId) return;
    await runAction("save", () =>
      fetch(`${API}/api/documents/${selectedId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }),
    );
  }

  async function deleteCurrentDocument() {
    if (!selectedId) return;
    if (!confirm("Удалить документ и все связанные записи из баз данных?")) return;
    const deletingId = selectedId;
    await runAction("delete", () =>
      fetch(`${API}/api/documents/${deletingId}`, { method: "DELETE" }),
    );
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.delete(deletingId);
      return next;
    });
    setSelectedId(null);
  }

  async function deleteSelectedDocuments() {
    const ids = Array.from(selectedIds);
    if (!ids.length) return;
    if (!confirm(`Удалить выбранные документы (${ids.length}) и все связанные записи?`)) {
      return;
    }
    setBusyAction("bulk-delete");
    setMessage(null);
    const response = await fetch(`${API}/api/documents/bulk-delete`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: ids, delete_files: true }),
    }).catch(() => null);
    if (!response?.ok) {
      setMessage("Ошибка массового удаления");
    } else {
      const data = await response.json();
      setMessage(`Удалено документов: ${data.deleted}; не найдено: ${data.missing}`);
      setSelectedIds(new Set());
      setSelectedId(null);
    }
    setBusyAction(null);
    await loadDocuments();
  }

  function toggleSelection(id: string, checked: boolean) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function createLink() {
    if (!selectedId || !linkedEntityId.trim()) return;
    await runAction("link", () =>
      fetch(`${API}/api/documents/${selectedId}/links`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          linked_entity_type: linkedEntityType,
          linked_entity_id: linkedEntityId.trim(),
          link_type: linkType,
        }),
      }),
    );
    setLinkedEntityId("");
    await loadDependencies(selectedId);
  }

  async function deleteLink(linkId: string) {
    if (!selectedId) return;
    await runAction("unlink", () =>
      fetch(`${API}/api/documents/${selectedId}/links/${linkId}`, {
        method: "DELETE",
      }),
    );
    await loadDependencies(selectedId);
  }

  const selected = summary?.document ?? documents.find((item) => item.id === selectedId);
  const pipeline = summary?.pipeline;

  return (
    <div className="flex h-full min-h-screen bg-slate-950 text-slate-100">
      <aside className="w-[420px] shrink-0 border-r border-slate-800 bg-slate-900">
        <div className="border-b border-slate-800 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h1 className="text-lg font-semibold">Документы</h1>
              <p className="mt-1 text-xs text-slate-400">
                Загрузка, распознавание, базы данных, память и связи.
              </p>
            </div>
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="rounded-md bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {uploading ? "Загрузка" : "Загрузить"}
            </button>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => uploadFiles(event.target.files)}
          />
          <div
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              uploadFiles(event.dataTransfer.files);
            }}
            onClick={() => fileInputRef.current?.click()}
            className={`mt-4 cursor-pointer rounded-md border border-dashed p-5 text-center transition ${
              dragging
                ? "border-blue-400 bg-blue-950/40 text-blue-100"
                : "border-slate-700 bg-slate-950/40 text-slate-300 hover:border-slate-500"
            }`}
          >
            <div className="text-sm font-medium">
              Перетащите файлы сюда или нажмите для выбора
            </div>
            <div className="mt-1 text-xs text-slate-500">
              PDF, JPG/PNG/TIFF, DOCX, XLSX, TXT, DXF, STEP/STP, XML/CSV/JSON
            </div>
            {uploading && (
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-slate-800">
                <div className="h-full w-2/3 animate-pulse rounded-full bg-blue-500" />
              </div>
            )}
          </div>
          <div className="mt-4 grid grid-cols-2 gap-2">
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="rounded-md border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
            >
              {STATUS_FILTERS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            <select
              value={docType}
              onChange={(event) => setDocType(event.target.value)}
              className="rounded-md border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
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
              onChange={(event) => setSourceChannel(event.target.value)}
              placeholder="Источник"
              className="rounded-md border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
            />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Поиск"
              className="rounded-md border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
            />
          </div>
          {message && (
            <div className="mt-3 rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-300">
              {message}
            </div>
          )}
          {uploadResults.length > 0 && (
            <div className="mt-3 space-y-1 rounded-md border border-slate-800 bg-slate-950 p-2">
              {uploadResults.map((item) => (
                <div
                  key={`${item.fileName}-${item.status}`}
                  className="flex items-start justify-between gap-3 rounded px-2 py-1.5 text-xs"
                >
                  <span className="min-w-0 truncate text-slate-300">{item.fileName}</span>
                  <span
                    className={
                      item.status === "uploaded"
                        ? "text-emerald-300"
                        : item.status === "quarantined"
                          ? "text-amber-300"
                          : "text-red-300"
                    }
                  >
                    {item.status === "uploaded"
                      ? "Загружен"
                      : item.status === "quarantined"
                        ? "Карантин"
                        : "Ошибка"}
                    {item.detail ? `: ${item.detail}` : ""}
                  </span>
                </div>
              ))}
            </div>
          )}
          {selectedIds.size > 0 && (
            <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-red-900 bg-red-950/30 px-3 py-2">
              <span className="text-xs text-red-100">
                Выбрано: {selectedIds.size}
              </span>
              <button
                onClick={deleteSelectedDocuments}
                disabled={Boolean(busyAction)}
                className="rounded-md bg-red-700 px-3 py-1.5 text-xs text-white hover:bg-red-600 disabled:opacity-50"
              >
                Удалить выбранные
              </button>
            </div>
          )}
        </div>
        <div className="h-[calc(100vh-260px)] overflow-y-auto">
          {documents.map((doc) => (
            <div
              key={doc.id}
              className={`flex w-full gap-3 border-b border-slate-800 px-4 py-3 text-left hover:bg-slate-800 ${
                selectedId === doc.id ? "bg-slate-800" : ""
              }`}
            >
              <input
                type="checkbox"
                checked={selectedIds.has(doc.id)}
                onChange={(event) => toggleSelection(doc.id, event.target.checked)}
                className="mt-1 shrink-0"
              />
              <button
                onClick={() => setSelectedId(doc.id)}
                className="min-w-0 flex-1 text-left"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{doc.file_name}</span>
                  <span className="rounded bg-slate-950 px-2 py-0.5 text-[11px] text-slate-400">
                    {doc.status}
                  </span>
                </div>
                <div className="mt-1 flex items-center gap-2 text-xs text-slate-500">
                  <span>{doc.doc_type ?? "не классифицирован"}</span>
                  <span>{fmtBytes(doc.file_size)}</span>
                  <span>{new Date(doc.created_at).toLocaleDateString("ru-RU")}</span>
                </div>
              </button>
            </div>
          ))}
          {!documents.length && (
            <div className="p-8 text-center text-sm text-slate-500">
              Документы не найдены
            </div>
          )}
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto p-6">
        {!selected ? (
          <div className="text-slate-500">Выберите документ</div>
        ) : (
          <div className="mx-auto max-w-6xl space-y-6">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h2 className="truncate text-xl font-semibold">{selected.file_name}</h2>
                <p className="mt-1 text-sm text-slate-400">
                  {selected.mime_type} · {fmtBytes(selected.file_size)} ·{" "}
                  {selected.source_channel ?? "upload"}
                </p>
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <Link
                  href={`/documents/${selected.id}/review`}
                  className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600"
                >
                  Review
                </Link>
                <a
                  href={`${API}/api/documents/${selected.id}/download`}
                  className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600"
                >
                  Скачать
                </a>
              </div>
            </div>

            <section className="grid grid-cols-1 gap-4 lg:grid-cols-4">
              <Metric label="Извлечений" value={pipeline?.extraction_count ?? 0} />
              <Metric label="Чанков памяти" value={pipeline?.memory_chunks ?? 0} />
              <Metric label="Узлов графа" value={pipeline?.graph_nodes ?? 0} />
              <Metric label="НТД замечаний" value={pipeline?.ntd_open_findings ?? 0} />
            </section>

            <section className="rounded-md border border-slate-800 bg-slate-900 p-4">
              <h3 className="text-sm font-semibold">Lifecycle и категоризация</h3>
              <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
                <label className="text-xs text-slate-400">
                  Тип документа
                  <select
                    value={selected.doc_type ?? ""}
                    onChange={(event) =>
                      updateDocument({ doc_type: event.target.value || null })
                    }
                    className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                  >
                    {DOC_TYPES.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-slate-400">
                  Статус
                  <select
                    value={selected.status}
                    onChange={(event) => updateDocument({ status: event.target.value })}
                    className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                  >
                    {STATUS_FILTERS.filter((item) => item.value).map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-slate-400">
                  Источник
                  <input
                    value={selected.source_channel ?? ""}
                    onChange={(event) =>
                      updateDocument({ source_channel: event.target.value || null })
                    }
                    className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                  />
                </label>
              </div>
            </section>

            <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <ActionPanel
                title="Распознавание и извлечение"
                status={[
                  pipeline?.processing_status,
                  pipeline?.current_step,
                  pipeline?.processing_error,
                ].filter(Boolean).join(" · ") || "нет активной задачи"}
                actions={[
                  {
                    label: "Классифицировать",
                    onClick: () =>
                      runAction("classify", () =>
                        fetch(`${API}/api/documents/${selected.id}/classify`, {
                          method: "POST",
                        }),
                      ),
                  },
                  {
                    label: "Распознать заново",
                    onClick: () =>
                      runAction("extract", () =>
                        fetch(`${API}/api/documents/${selected.id}/extract`, {
                          method: "POST",
                        }),
                      ),
                  },
                ]}
                busyAction={busyAction}
              />
              <ActionPanel
                title="Память и граф"
                status={
                  pipeline?.graph_status
                    ? `${pipeline.graph_status} · ${pipeline.graph_scope ?? "scope"}`
                    : "граф еще не построен"
                }
                actions={[
                  {
                    label: "Compact graph",
                    onClick: () =>
                      runAction("memory", () =>
                        fetch(`${API}/api/documents/${selected.id}/memory/rebuild?build_scope=compact`, {
                          method: "POST",
                        }),
                      ),
                  },
                  {
                    label: "Extended graph",
                    onClick: () =>
                      runAction("memory", () =>
                        fetch(`${API}/api/documents/${selected.id}/memory/rebuild?build_scope=extended`, {
                          method: "POST",
                        }),
                      ),
                  },
                ]}
                busyAction={busyAction}
              />
              <ActionPanel
                title="НТД и базы данных"
                status={`Проверок: ${pipeline?.ntd_checks ?? 0}, открыто: ${pipeline?.ntd_open_findings ?? 0}`}
                actions={[
                  {
                    label: "Проверить НТД",
                    onClick: () =>
                      runAction("ntd", () =>
                        fetch(`${API}/api/documents/${selected.id}/ntd-check`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({
                            document_id: selected.id,
                            triggered_by: "manual",
                            actor: "user",
                          }),
                        }),
                      ),
                  },
                  {
                    label: "Создать НТД",
                    onClick: () =>
                      runAction("ntd-source", () =>
                        fetch(`${API}/api/ntd/documents/from-source`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({
                            source_document_id: selected.id,
                            index_immediately: true,
                          }),
                        }),
                      ),
                  },
                ]}
                busyAction={busyAction}
              />
              <ActionPanel
                title="Управление"
                status={`Связей: ${summary?.links.length ?? 0}, артефактов: ${pipeline?.artifact_count ?? 0}`}
                actions={[
                  {
                    label: "Удалить полностью",
                    danger: true,
                    onClick: deleteCurrentDocument,
                  },
                ]}
                busyAction={busyAction}
              />
            </section>

            <section className="rounded-md border border-slate-800 bg-slate-900 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold">Связи и зависимости</h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Явные связи, узлы памяти и найденные ребра графа.
                  </p>
                </div>
                <div className="flex gap-2">
                  <input
                    value={dependencyQuery}
                    onChange={(event) => setDependencyQuery(event.target.value)}
                    placeholder="Поиск по связям"
                    className="w-56 rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                  />
                  <button
                    onClick={() => loadDependencies(selected.id)}
                    className="rounded-md bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600"
                  >
                    Найти
                  </button>
                </div>
              </div>
              <div className="mt-4 grid grid-cols-1 gap-2 md:grid-cols-[1fr_1fr_2fr_auto]">
                <input
                  value={linkType}
                  onChange={(event) => setLinkType(event.target.value)}
                  placeholder="Тип связи"
                  className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                />
                <input
                  value={linkedEntityType}
                  onChange={(event) => setLinkedEntityType(event.target.value)}
                  placeholder="Тип сущности"
                  className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                />
                <input
                  value={linkedEntityId}
                  onChange={(event) => setLinkedEntityId(event.target.value)}
                  placeholder="UUID связанной сущности"
                  className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                />
                <button
                  onClick={createLink}
                  disabled={Boolean(busyAction) || !linkedEntityId.trim()}
                  className="rounded-md bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  Добавить
                </button>
              </div>
              <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
                <Metric label="Явных связей" value={summary?.links.length ?? 0} />
                <Metric label="Узлов найдено" value={dependencies?.total_nodes ?? 0} />
                <Metric label="Ребер найдено" value={dependencies?.total_edges ?? 0} />
              </div>
              <div className="mt-3 divide-y divide-slate-800">
                {summary?.links.map((link) => (
                  <div
                    key={link.id}
                    className="flex items-center justify-between gap-3 py-2 text-sm text-slate-300"
                  >
                    <div className="min-w-0">
                      {link.link_type}: {link.linked_entity_type}{" "}
                      <span className="font-mono text-xs text-slate-500">
                        {link.linked_entity_id}
                      </span>
                    </div>
                    <button
                      onClick={() => deleteLink(link.id)}
                      disabled={Boolean(busyAction)}
                      className="rounded-md bg-red-950 px-2 py-1 text-xs text-red-200 hover:bg-red-900 disabled:opacity-50"
                    >
                      Удалить
                    </button>
                  </div>
                ))}
                {!summary?.links.length && (
                  <p className="py-4 text-sm text-slate-500">
                    Явных связей пока нет. Их можно добавить вручную или получить
                    после распознавания и построения графа.
                  </p>
                )}
              </div>
              <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
                <DependencyList
                  title="Узлы памяти"
                  items={(dependencies?.nodes ?? []).map((node) => ({
                    id: node.id,
                    title: `${node.node_type}: ${node.title}`,
                    detail: node.summary ?? `confidence ${node.confidence.toFixed(2)}`,
                  }))}
                />
                <DependencyList
                  title="Ребра графа"
                  items={(dependencies?.edges ?? []).map((edge) => ({
                    id: edge.id,
                    title: edge.edge_type,
                    detail:
                      edge.reason ??
                      `${edge.source_node_id.slice(0, 8)} → ${edge.target_node_id.slice(0, 8)}`,
                  }))}
                />
              </div>
            </section>
          </div>
        )}
      </main>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
    </div>
  );
}

function ActionPanel({
  title,
  status,
  actions,
  busyAction,
}: {
  title: string;
  status: string;
  actions: Array<{ label: string; danger?: boolean; onClick: () => void }>;
  busyAction: string | null;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mt-1 min-h-5 text-xs text-slate-500">{status}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {actions.map((action) => (
          <button
            key={action.label}
            onClick={action.onClick}
            disabled={Boolean(busyAction)}
            className={`rounded-md px-3 py-2 text-sm disabled:opacity-50 ${
              action.danger
                ? "bg-red-950 text-red-200 hover:bg-red-900"
                : "bg-slate-700 text-slate-100 hover:bg-slate-600"
            }`}
          >
            {busyAction ? "Выполняется..." : action.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function DependencyList({
  title,
  items,
}: {
  title: string;
  items: Array<{ id: string; title: string; detail: string }>;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950 p-3">
      <h4 className="text-xs font-semibold uppercase text-slate-500">
        {title}
      </h4>
      <div className="mt-2 max-h-80 overflow-y-auto divide-y divide-slate-800">
        {items.map((item) => (
          <div key={item.id} className="py-2">
            <div className="truncate text-sm text-slate-200">{item.title}</div>
            <div className="mt-1 line-clamp-2 text-xs text-slate-500">
              {item.detail}
            </div>
          </div>
        ))}
        {!items.length && (
          <div className="py-4 text-sm text-slate-600">Нет данных</div>
        )}
      </div>
    </div>
  );
}
