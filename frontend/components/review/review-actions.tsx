"use client";

import { useState } from "react";

interface ReviewActionsProps {
  status: string;
  onApprove: (comment?: string) => void;
  onReject: (reason: string) => void;
  onReExtract: () => void;
  onValidate: () => void;
  onReceive?: () => void;
  onSchedulePayment?: () => void;
  onExportExcel?: () => void;
  onExport1C?: () => void;
  loading?: boolean;
}

export function ReviewActions({
  status,
  onApprove,
  onReject,
  onReExtract,
  onValidate,
  onReceive,
  onSchedulePayment,
  onExportExcel,
  onExport1C,
  loading = false,
}: ReviewActionsProps) {
  const [showReject, setShowReject] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  const canDecide =
    status === "needs_review" || status === "draft" || status === "ingested";

  return (
    <div className="bg-white border border-slate-200 rounded-lg p-4 space-y-3">
      <h3 className="text-sm font-semibold">Действия</h3>

      {canDecide ? (
        <>
          {!showReject ? (
            <div className="flex gap-2">
              <button
                onClick={() => onApprove()}
                disabled={loading}
                className="flex-1 px-3 py-2 text-sm bg-green-500 text-white rounded-md hover:bg-green-600 disabled:opacity-50 font-medium"
              >
                Утвердить <kbd className="ml-1 text-xs opacity-70">a</kbd>
              </button>
              <button
                onClick={() => setShowReject(true)}
                disabled={loading}
                className="flex-1 px-3 py-2 text-sm bg-red-500 text-white rounded-md hover:bg-red-600 disabled:opacity-50 font-medium"
              >
                Отклонить <kbd className="ml-1 text-xs opacity-70">r</kbd>
              </button>
            </div>
          ) : (
            <div className="space-y-2">
              <textarea
                autoFocus
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="Причина отклонения..."
                className="w-full text-sm border rounded-md px-3 py-2 focus:outline-none focus:ring-1 focus:ring-red-400 resize-none"
                rows={2}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    setShowReject(false);
                    setRejectReason("");
                  }
                }}
              />
              <div className="flex gap-2">
                <button
                  onClick={() => {
                    if (rejectReason.trim()) {
                      onReject(rejectReason.trim());
                      setShowReject(false);
                      setRejectReason("");
                    }
                  }}
                  disabled={!rejectReason.trim() || loading}
                  className="flex-1 px-3 py-1.5 text-sm bg-red-500 text-white rounded-md hover:bg-red-600 disabled:opacity-50"
                >
                  Подтвердить отклонение
                </button>
                <button
                  onClick={() => {
                    setShowReject(false);
                    setRejectReason("");
                  }}
                  className="px-3 py-1.5 text-sm border rounded-md hover:bg-slate-50"
                >
                  Отмена
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <div
          className={`text-sm px-3 py-2 rounded-md text-center font-medium ${
            status === "approved"
              ? "bg-green-50 text-green-700"
              : status === "rejected"
                ? "bg-red-50 text-red-700"
                : "bg-slate-50 text-slate-600"
          }`}
        >
          {status === "approved"
            ? "Утверждён"
            : status === "rejected"
              ? "Отклонён"
              : status}
        </div>
      )}

      <div className="border-t border-slate-100 pt-3 flex gap-2">
        <button
          onClick={onValidate}
          disabled={loading}
          className="flex-1 px-3 py-1.5 text-xs border border-slate-200 rounded-md hover:bg-slate-50 disabled:opacity-50"
        >
          Проверить суммы
        </button>
        <button
          onClick={onReExtract}
          disabled={loading}
          className="flex-1 px-3 py-1.5 text-xs border border-slate-200 rounded-md hover:bg-slate-50 disabled:opacity-50"
        >
          Переизвлечь
        </button>
      </div>

      {status === "approved" &&
        (onReceive || onSchedulePayment || onExportExcel || onExport1C) && (
          <div className="border-t border-slate-100 pt-3 space-y-2">
            <p className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">
              Действия после утверждения
            </p>
            {onReceive && (
              <button
                onClick={onReceive}
                disabled={loading}
                className="w-full px-3 py-2 text-sm bg-indigo-600 text-white rounded-md hover:bg-indigo-500 disabled:opacity-50 font-medium"
              >
                📦 Оприходовать на склад
              </button>
            )}
            {onSchedulePayment && (
              <button
                onClick={onSchedulePayment}
                disabled={loading}
                className="w-full px-3 py-2 text-sm bg-emerald-600 text-white rounded-md hover:bg-emerald-500 disabled:opacity-50 font-medium"
              >
                💳 Запланировать оплату
              </button>
            )}
            {(onExportExcel || onExport1C) && (
              <div className="flex gap-2">
                {onExportExcel && (
                  <button
                    onClick={onExportExcel}
                    disabled={loading}
                    className="flex-1 px-3 py-1.5 text-xs border border-slate-200 rounded-md hover:bg-slate-50 disabled:opacity-50"
                  >
                    Excel
                  </button>
                )}
                {onExport1C && (
                  <button
                    onClick={onExport1C}
                    disabled={loading}
                    className="flex-1 px-3 py-1.5 text-xs border border-amber-300 text-amber-700 rounded-md hover:bg-amber-50 disabled:opacity-50"
                    title="Требует подтверждения"
                  >
                    1С ⚠
                  </button>
                )}
              </div>
            )}
          </div>
        )}
    </div>
  );
}
