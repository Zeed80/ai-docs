"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { PdfViewer, type BBox } from "@/components/review/pdf-viewer";
import { ExtractionPanel } from "@/components/review/extraction-panel";
import { ReviewActions } from "@/components/review/review-actions";
import {
  documents as docsApi,
  extraction as extractionApi,
  invoices as invoicesApi,
  ntd as ntdApi,
  type Document,
  type InvoiceDetail,
  type NTDCheckAvailability,
  type NTDCheck,
  type NTDFinding,
  type ValidationResponse,
} from "@/lib/api-client";
import { LineItemsTable } from "@/components/review/line-items-table";
import {
  advanceStreak,
  getQueue,
  loadReviewQueue,
  setCurrentIndex,
  subscribeReviewQueue,
  type ReviewQueue,
} from "@/lib/review-streak";

type ReviewDoc = Document;

export default function ReviewPage() {
  const params = useParams();
  const router = useRouter();
  const documentId = params.id as string;

  const [doc, setDoc] = useState<ReviewDoc | null>(null);
  const [invoiceDetail, setInvoiceDetail] = useState<InvoiceDetail | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [activeField, setActiveField] = useState<string | null>(null);
  const [validation, setValidation] = useState<ValidationResponse | null>(null);
  const [ntdChecks, setNtdChecks] = useState<NTDCheck[]>([]);
  const [ntdAvailability, setNtdAvailability] =
    useState<NTDCheckAvailability | null>(null);
  const [ntdFindings, setNtdFindings] = useState<NTDFinding[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [streak, setStreak] = useState<ReviewQueue>(getQueue());
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function showToast(msg: string, duration = 2000) {
    setToast(msg);
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), duration);
  }

  // Subscribe to streak updates
  useEffect(() => {
    const unsub = subscribeReviewQueue(setStreak);
    return unsub;
  }, []);

  // Init streak queue on first mount
  useEffect(() => {
    loadReviewQueue().then(() => {
      setCurrentIndex(documentId);
    });
  }, [documentId]);

  // Fetch document
  const fetchDoc = useCallback(async () => {
    try {
      const data = await docsApi.get(documentId);
      setDoc(data);
    } catch {
      setDoc(null);
    } finally {
      setLoading(false);
    }
  }, [documentId]);

  // Fetch invoice lines (best-effort)
  const fetchInvoice = useCallback(async () => {
    try {
      const data = await docsApi.getInvoice(documentId);
      setInvoiceDetail(data);
    } catch {
      setInvoiceDetail(null);
    }
  }, [documentId]);

  useEffect(() => {
    setLoading(true);
    setValidation(null);
    setNtdChecks([]);
    setNtdFindings([]);
    setActiveField(null);
    setInvoiceDetail(null);
    fetchDoc();
    fetchInvoice();
  }, [fetchDoc, fetchInvoice]);

  useEffect(() => {
    ntdApi
      .listChecks(documentId)
      .then(setNtdChecks)
      .catch(() => setNtdChecks([]));
    ntdApi
      .availability(documentId)
      .then(setNtdAvailability)
      .catch(() => setNtdAvailability(null));
  }, [documentId]);

  // Auto-focus first low-confidence field
  useEffect(() => {
    if (!doc) return;
    const ext = doc.extractions[0];
    if (!ext) return;
    const lowField = ext.fields.find(
      (f) => f.confidence != null && f.confidence < 0.6,
    );
    if (lowField) {
      setActiveField(lowField.field_name);
    }
  }, [doc]);

  // Build bbox map
  const bboxes: Record<string, BBox> = {};
  const ext = doc?.extractions[0];
  if (ext) {
    for (const f of ext.fields) {
      if (f.field_name && f.bbox_page != null) {
        bboxes[f.field_name] = {
          page: f.bbox_page,
          x: f.bbox_x ?? 0,
          y: f.bbox_y ?? 0,
          w: f.bbox_w ?? 0,
          h: f.bbox_h ?? 0,
        };
      }
    }
  }

  const highlightedBbox = activeField ? (bboxes[activeField] ?? null) : null;

  // Navigate to next doc in streak
  function goToNext() {
    const nextId = advanceStreak();
    if (nextId) {
      router.push(`/documents/${nextId}/review`);
    } else {
      showToast(
        `Серия завершена! ${streak.streakCount + 1} документов проверено`,
        3000,
      );
      setTimeout(() => router.push("/inbox"), 1500);
    }
  }

  // Keyboard shortcuts
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;

      switch (e.key) {
        case "Escape":
          router.push(`/documents/${documentId}`);
          break;
        case "a":
          if (!e.ctrlKey && !e.metaKey) handleApprove();
          break;
        case "r":
          if (!e.ctrlKey && !e.metaKey) {
            showToast("Нажмите кнопку Отклонить для ввода причины");
          }
          break;
        case "j": {
          e.preventDefault();
          if (!ext) break;
          const fields = ext.fields;
          const idx = fields.findIndex((f) => f.field_name === activeField);
          const next = idx < fields.length - 1 ? idx + 1 : 0;
          setActiveField(fields[next].field_name);
          break;
        }
        case "k": {
          e.preventDefault();
          if (!ext) break;
          const fields2 = ext.fields;
          const idx2 = fields2.findIndex((f) => f.field_name === activeField);
          const prev = idx2 > 0 ? idx2 - 1 : fields2.length - 1;
          setActiveField(fields2[prev].field_name);
          break;
        }
        case "n": {
          // Skip to next without decision
          if (!e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            const nextId = streak.ids[streak.index + 1];
            if (nextId) router.push(`/documents/${nextId}/review`);
          }
          break;
        }
      }
    }

    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  });

  async function handleApprove() {
    if (!doc || actionLoading) return;
    setActionLoading(true);
    try {
      await docsApi.update(documentId, {
        status: "approved",
      } as Partial<Document>);
      showToast(`Утверждён! Серия: ${streak.streakCount + 1}`);
      goToNext();
    } catch {
      showToast("Ошибка утверждения");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleReject(reason: string) {
    if (!doc || actionLoading) return;
    setActionLoading(true);
    try {
      await docsApi.update(documentId, {
        status: "rejected",
      } as Partial<Document>);
      showToast(`Отклонён. Серия: ${streak.streakCount + 1}`);
      goToNext();
    } catch {
      showToast("Ошибка отклонения");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleCorrect(fieldName: string, value: string) {
    try {
      await extractionApi.correctField(documentId, {
        field_name: fieldName,
        corrected_value: value,
      });
      showToast(`Поле "${fieldName}" исправлено`);
      await fetchDoc();
    } catch {
      showToast("Ошибка сохранения исправления");
    }
  }

  async function handleValidate() {
    setActionLoading(true);
    try {
      const invoiceId = getInvoiceId();
      if (!invoiceId) {
        showToast("Счёт доступен только после утверждения документа");
        return;
      }
      const result = await invoicesApi.validate(invoiceId);
      setValidation(result);
      showToast(
        result.is_valid
          ? "Все суммы верны"
          : `Найдено ошибок: ${result.errors.length}`,
        3000,
      );
    } catch {
      showToast("Ошибка валидации");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleReExtract() {
    setActionLoading(true);
    try {
      const task = await extractionApi.extract(documentId);
      showToast(
        `Переизвлечение запущено (task: ${task.task_id.slice(0, 8)}...)`,
      );
    } catch {
      showToast("Ошибка запуска переизвлечения");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleNtdCheck() {
    if (!doc || actionLoading) return;
    setActionLoading(true);
    try {
      const result = await ntdApi.runCheck(documentId);
      setNtdChecks((items) => [result.check, ...items]);
      setNtdFindings(result.findings);
      setNtdAvailability((current) =>
        current ? { ...current, can_check: true, reasons: [] } : current,
      );
      showToast(
        result.check.findings_total
          ? `НТД: замечаний ${result.check.findings_total}`
          : "НТД: замечаний нет",
        3000,
      );
    } catch {
      showToast("НТД: проверка недоступна");
    } finally {
      setActionLoading(false);
    }
  }

  function getInvoiceId(): string | null {
    return invoiceDetail?.id ?? null;
  }

  async function handleReceive() {
    const invoiceId = getInvoiceId();
    if (!invoiceId) {
      showToast("Счёт не найден — обработайте документ сначала");
      return;
    }
    setActionLoading(true);
    try {
      const API = getApiBaseUrl();
      const res = await fetch(`${API}/api/invoices/${invoiceId}/receive`, {
        method: "POST",
      });
      if (!res.ok) {
        const d = await res.json();
        showToast(d.detail ?? "Ошибка создания ордера");
        return;
      }
      const data = await res.json();
      showToast(`Создан ордер ${data.receipt_number} (Ожидается)`, 3000);
      router.push(`/warehouse/receipts/${data.receipt_id}`);
    } catch {
      showToast("Ошибка сети");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleSchedulePayment() {
    const invoiceId = getInvoiceId();
    if (!invoiceId) {
      showToast("Счёт не найден");
      return;
    }
    setActionLoading(true);
    try {
      const API = getApiBaseUrl();
      const res = await fetch(
        `${API}/api/invoices/${invoiceId}/schedule-payment`,
        { method: "POST" },
      );
      if (!res.ok) {
        const d = await res.json();
        showToast(d.detail ?? "Ошибка планирования оплаты");
        return;
      }
      showToast("Оплата запланирована", 3000);
    } catch {
      showToast("Ошибка сети");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleExportExcel() {
    const invoiceId = getInvoiceId();
    if (!invoiceId) {
      showToast("Счёт не найден");
      return;
    }
    setActionLoading(true);
    try {
      const API = getApiBaseUrl();
      const res = await fetch(`${API}/api/invoices/${invoiceId}/export`, {
        method: "POST",
      });
      if (!res.ok) {
        const d = await res.json();
        showToast(d.detail ?? "Ошибка экспорта");
        return;
      }
      const data = await res.json();
      showToast(
        `Экспорт Excel запущен (${data.job_id?.slice(0, 8) ?? "ok"})`,
        3000,
      );
    } catch {
      showToast("Ошибка сети");
    } finally {
      setActionLoading(false);
    }
  }

  async function handleExport1C() {
    const invoiceId = getInvoiceId();
    if (!invoiceId) {
      showToast("Счёт не найден");
      return;
    }
    setActionLoading(true);
    try {
      const API = getApiBaseUrl();
      const res = await fetch(`${API}/api/invoices/${invoiceId}/export-1c`, {
        method: "POST",
      });
      if (!res.ok) {
        const d = await res.json();
        showToast(d.detail ?? "Ошибка экспорта 1С");
        return;
      }
      const data = await res.json();
      showToast(
        `Экспорт 1С поставлен в очередь (${data.job_id?.slice(0, 8) ?? "ok"})`,
        3000,
      );
    } catch {
      showToast("Ошибка сети");
    } finally {
      setActionLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-slate-400">
        Загрузка...
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="flex items-center justify-center h-full text-slate-400">
        Документ не найден
      </div>
    );
  }

  const queueRemaining = streak.ids.length;
  const reasonText: Record<string, string> = {
    document_quarantined: "Документ в карантине",
    document_has_no_text: "Нет извлеченного текста",
    ntd_requirements_not_configured: "Нет активных требований НТД",
  };
  const ntdDisabledReason = ntdAvailability
    ? ntdAvailability.reasons
        .map((reason) => reasonText[reason] ?? reason)
        .join("; ") || null
    : doc.status === "suspicious"
      ? "Документ в карантине"
      : !ext && doc.status === "ingested"
        ? "Сначала обработайте документ"
        : null;

  return (
    <div className="flex flex-col h-screen bg-slate-950 text-slate-100">
      {/* Top bar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-slate-800 bg-slate-900">
        <div className="flex items-center gap-3">
          <button
            onClick={() => router.push(`/documents/${documentId}`)}
            className="text-sm text-slate-400 hover:text-slate-200"
          >
            &larr; Назад
          </button>
          <h1 className="text-sm font-semibold truncate max-w-md text-slate-100">
            {doc.file_name}
          </h1>
          <span
            className={`px-2 py-0.5 text-xs font-medium rounded-full ${
              doc.status === "needs_review"
                ? "bg-amber-900/50 text-amber-300"
                : doc.status === "approved"
                  ? "bg-green-900/50 text-green-300"
                  : doc.status === "rejected"
                    ? "bg-red-900/50 text-red-300"
                    : "bg-slate-800 text-slate-400"
            }`}
          >
            {doc.status}
          </span>
        </div>

        <div className="flex items-center gap-4">
          {/* Streak counter */}
          {streak.streakCount > 0 && (
            <div className="flex items-center gap-1.5 px-2.5 py-1 bg-emerald-900/50 text-emerald-300 rounded-full text-xs font-medium">
              <span className="text-base">&#9889;</span>
              Серия: {streak.streakCount}
            </div>
          )}
          {queueRemaining > 0 && (
            <span className="text-xs text-slate-500">
              Осталось: {queueRemaining}
            </span>
          )}
          <div className="text-xs text-slate-500">
            <kbd className="px-1 border border-slate-700 rounded text-[10px] bg-slate-800">
              j
            </kbd>
            /
            <kbd className="px-1 border border-slate-700 rounded text-[10px] bg-slate-800">
              k
            </kbd>{" "}
            поля
            <kbd className="px-1 border border-slate-700 rounded text-[10px] bg-slate-800 ml-2">
              a
            </kbd>{" "}
            утвердить
            <kbd className="px-1 border border-slate-700 rounded text-[10px] bg-slate-800 ml-2">
              n
            </kbd>{" "}
            пропустить
            <kbd className="px-1 border border-slate-700 rounded text-[10px] bg-slate-800 ml-2">
              Esc
            </kbd>{" "}
            выход
          </div>
        </div>
      </div>

      {/* Validation errors banner */}
      {validation && !validation.is_valid && (
        <div className="px-4 py-2 bg-red-950/50 border-b border-red-800 text-sm text-red-300">
          <strong>Ошибки валидации:</strong>{" "}
          {validation.errors.map((e, i) => (
            <span key={i}>
              {e.field}: {e.message}
              {i < validation.errors.length - 1 ? " | " : ""}
            </span>
          ))}
        </div>
      )}

      {/* Split view */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left: PDF */}
        <div className="flex-1 p-3">
          <PdfViewer
            documentId={documentId}
            mimeType={doc.mime_type}
            highlightedBbox={highlightedBbox}
            bboxes={bboxes}
            activeField={activeField}
          />
        </div>

        {/* Right: Fields + Actions */}
        <div className="w-96 border-l border-slate-800 flex flex-col p-3 gap-3 overflow-auto bg-slate-950">
          <ReviewActions
            status={doc.status}
            onApprove={handleApprove}
            onReject={handleReject}
            onReExtract={handleReExtract}
            onValidate={handleValidate}
            onReceive={handleReceive}
            onSchedulePayment={handleSchedulePayment}
            onExportExcel={handleExportExcel}
            onExport1C={handleExport1C}
            loading={actionLoading}
          />

          <div className="rounded border border-slate-700 bg-slate-900 p-3">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-slate-100">
                  Нормоконтроль
                </h2>
                <p className="mt-0.5 text-xs text-slate-400">
                  {ntdChecks[0]
                    ? `${ntdChecks[0].summary ?? "Последняя проверка НТД выполнена"}`
                    : ntdAvailability?.mode === "auto"
                      ? "Автоматический режим включен; ручной запуск тоже доступен."
                      : "Ручной режим: проверка запускается по кнопке."}
                </p>
              </div>
              <button
                onClick={handleNtdCheck}
                disabled={actionLoading || Boolean(ntdDisabledReason)}
                title={ntdDisabledReason ?? "Проверить документ по базе НТД"}
                className="shrink-0 rounded border border-slate-600 px-2.5 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Проверить на соответствие НТД
              </button>
            </div>
            {ntdDisabledReason && (
              <p className="mt-2 text-xs text-amber-400">{ntdDisabledReason}</p>
            )}
            {ntdFindings.length > 0 && (
              <div className="mt-3 space-y-2">
                {ntdFindings.slice(0, 5).map((finding) => (
                  <div
                    key={finding.id}
                    className="rounded border border-amber-800 bg-amber-950/40 p-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-amber-300">
                        {finding.severity}
                      </span>
                      <span className="text-[11px] text-amber-400">
                        {Math.round(finding.confidence * 100)}%
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-slate-300">
                      {finding.message}
                    </p>
                    {finding.recommendation && (
                      <p className="mt-1 text-[11px] text-slate-500">
                        {finding.recommendation}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex-1 min-h-0">
            <ExtractionPanel
              fields={ext?.fields ?? []}
              overallConfidence={ext?.overall_confidence ?? null}
              activeField={activeField}
              onFieldFocus={setActiveField}
              onCorrect={handleCorrect}
              disabled={doc.status === "approved" || doc.status === "rejected"}
            />
          </div>

          {/* Invoice line items */}
          {invoiceDetail && (
            <div className="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden flex-shrink-0">
              <div className="px-3 py-2 border-b border-slate-700 bg-slate-800 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-100">
                  Товары и услуги
                  {invoiceDetail.preview && (
                    <span className="ml-2 text-[10px] text-amber-400 font-normal">
                      предпросмотр
                    </span>
                  )}
                </h3>
                <span className="text-xs text-slate-400">
                  {invoiceDetail.lines.length} поз.
                  {invoiceDetail.total_amount != null && (
                    <span className="ml-2 text-slate-300 font-medium">
                      {invoiceDetail.total_amount.toLocaleString("ru-RU", {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      })}{" "}
                      {invoiceDetail.currency ?? "₽"}
                    </span>
                  )}
                </span>
              </div>
              <div className="overflow-x-auto max-h-72 overflow-y-auto">
                <LineItemsTable
                  lines={invoiceDetail.lines}
                  currency={invoiceDetail.currency}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 bg-slate-800 text-white text-sm rounded-lg shadow-lg z-50">
          {toast}
        </div>
      )}
    </div>
  );
}
