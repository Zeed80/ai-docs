"use client";

import { useState } from "react";
import type { ExtractionField } from "@/lib/api-client";

interface ExtractionPanelProps {
  fields: ExtractionField[];
  overallConfidence: number | null;
  activeField: string | null;
  onFieldFocus: (fieldName: string) => void;
  onCorrect: (fieldName: string, value: string) => void;
  disabled?: boolean;
}

const FIELD_LABELS: Record<string, string> = {
  invoice_number: "Номер счёта",
  invoice_date: "Дата счёта",
  due_date: "Срок оплаты",
  validity_date: "Действует до",
  currency: "Валюта",
  payment_id: "Идент. платежа",
  notes: "Примечания",
  subtotal: "Итого без НДС",
  tax_amount: "НДС",
  total_amount: "Итого к оплате",
  "supplier.name": "Поставщик",
  "supplier.inn": "ИНН поставщика",
  "supplier.kpp": "КПП поставщика",
  "supplier.address": "Адрес поставщика",
  "supplier.phone": "Телефон поставщика",
  "supplier.email": "Email поставщика",
  "supplier.bank_name": "Банк",
  "supplier.bank_bik": "БИК",
  "supplier.bank_account": "Р/С",
  "supplier.corr_account": "К/С",
  "buyer.name": "Покупатель",
  "buyer.inn": "ИНН покупателя",
  "buyer.kpp": "КПП покупателя",
  "buyer.address": "Адрес покупателя",
};

function fieldLabel(name: string): string {
  if (FIELD_LABELS[name]) return FIELD_LABELS[name];
  if (name.match(/^line_\d+\.sku$/))
    return `Арт. (строка ${name.match(/\d+/)?.[0]})`;
  return name;
}

function confidenceColor(c: number | null): string {
  if (c == null) return "bg-slate-100 text-slate-500";
  if (c >= 0.8) return "bg-green-100 text-green-700";
  if (c >= 0.5) return "bg-amber-100 text-amber-700";
  return "bg-red-100 text-red-700";
}

function confidenceLabel(c: number | null): string {
  if (c == null) return "—";
  return `${(c * 100).toFixed(0)}%`;
}

export function ExtractionPanel({
  fields,
  overallConfidence,
  activeField,
  onFieldFocus,
  onCorrect,
  disabled = false,
}: ExtractionPanelProps) {
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  // Sort: low confidence first (auto-focus pattern)
  const sorted = [...fields].sort((a, b) => {
    const ca = a.confidence ?? 1;
    const cb = b.confidence ?? 1;
    return ca - cb;
  });

  const lowConfCount = fields.filter(
    (f) => f.confidence != null && f.confidence < 0.6,
  ).length;

  function startEdit(field: ExtractionField) {
    setEditingField(field.field_name);
    setEditValue(
      field.human_corrected
        ? (field.corrected_value ?? "")
        : (field.field_value ?? ""),
    );
  }

  function submitEdit(fieldName: string) {
    if (editValue.trim()) {
      onCorrect(fieldName, editValue.trim());
    }
    setEditingField(null);
  }

  return (
    <div className="bg-white border border-slate-200 rounded-lg overflow-hidden flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-200 bg-slate-50">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">Извлечённые поля</h3>
          {overallConfidence != null && (
            <span
              className={`text-xs px-2 py-0.5 rounded-full ${confidenceColor(overallConfidence)}`}
            >
              {confidenceLabel(overallConfidence)}
            </span>
          )}
        </div>
        {lowConfCount > 0 && (
          <p className="text-xs text-amber-600 mt-1">
            {lowConfCount}{" "}
            {lowConfCount === 1 ? "поле требует" : "полей требуют"} проверки
          </p>
        )}
      </div>

      {/* Fields list */}
      <div className="flex-1 overflow-auto">
        {sorted.length === 0 ? (
          <div className="p-4 text-sm text-slate-400 text-center">
            Нет извлечённых полей
          </div>
        ) : (
          <div className="divide-y divide-slate-100">
            {sorted.map((field) => {
              const isActive = activeField === field.field_name;
              const isEditing = editingField === field.field_name;
              const isLow = field.confidence != null && field.confidence < 0.6;

              return (
                <div
                  key={field.field_name}
                  className={`px-4 py-2.5 cursor-pointer transition-colors ${
                    isActive
                      ? "bg-blue-50 border-l-2 border-l-blue-500"
                      : isLow
                        ? "bg-amber-50/50 border-l-2 border-l-amber-400"
                        : "border-l-2 border-l-transparent hover:bg-slate-50"
                  }`}
                  onClick={() => onFieldFocus(field.field_name)}
                >
                  <div className="flex items-center justify-between mb-0.5">
                    <span className="text-xs text-slate-500">
                      {fieldLabel(field.field_name)}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {field.human_corrected && (
                        <span className="text-[10px] px-1 py-0.5 bg-blue-100 text-blue-600 rounded">
                          исправлено
                        </span>
                      )}
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded-full ${confidenceColor(field.confidence)}`}
                      >
                        {confidenceLabel(field.confidence)}
                      </span>
                    </div>
                  </div>

                  {isEditing ? (
                    <div className="flex gap-1 mt-1">
                      <input
                        autoFocus
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") submitEdit(field.field_name);
                          if (e.key === "Escape") setEditingField(null);
                        }}
                        className="flex-1 text-sm border rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-400"
                      />
                      <button
                        onClick={() => submitEdit(field.field_name)}
                        className="px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600"
                      >
                        OK
                      </button>
                    </div>
                  ) : (
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium">
                        {field.human_corrected
                          ? field.corrected_value
                          : (field.field_value ?? "—")}
                      </span>
                      {!disabled && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            startEdit(field);
                          }}
                          className="text-xs text-slate-400 hover:text-blue-500 ml-2 opacity-0 group-hover:opacity-100"
                          style={{ opacity: isActive ? 1 : undefined }}
                          title="Исправить (e)"
                        >
                          &#9998;
                        </button>
                      )}
                    </div>
                  )}

                  {field.confidence_reason && (
                    <span className="text-[10px] text-slate-400 mt-0.5 block">
                      {field.confidence_reason}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
