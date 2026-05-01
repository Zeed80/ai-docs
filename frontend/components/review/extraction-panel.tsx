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
  "supplier.phone": "Телефон",
  "supplier.email": "Email",
  "supplier.bank_name": "Банк",
  "supplier.bank_bik": "БИК",
  "supplier.bank_account": "Р/С",
  "supplier.corr_account": "К/С",
  "buyer.name": "Покупатель",
  "buyer.inn": "ИНН покупателя",
  "buyer.kpp": "КПП покупателя",
  "buyer.address": "Адрес покупателя",
};

const GROUPS: {
  key: string;
  label: string;
  fields: string[];
  collapsible?: boolean;
}[] = [
  {
    key: "main",
    label: "Основное",
    fields: [
      "invoice_number",
      "invoice_date",
      "due_date",
      "total_amount",
      "currency",
      "subtotal",
      "tax_amount",
    ],
  },
  {
    key: "supplier",
    label: "Поставщик",
    fields: [
      "supplier.name",
      "supplier.inn",
      "supplier.kpp",
      "supplier.address",
      "supplier.phone",
      "supplier.email",
    ],
  },
  {
    key: "buyer",
    label: "Покупатель",
    fields: ["buyer.name", "buyer.inn", "buyer.kpp", "buyer.address"],
  },
  {
    key: "bank",
    label: "Реквизиты",
    fields: [
      "supplier.bank_name",
      "supplier.bank_bik",
      "supplier.bank_account",
      "supplier.corr_account",
    ],
    collapsible: true,
  },
  {
    key: "other",
    label: "Прочее",
    fields: ["notes", "payment_id", "validity_date"],
    collapsible: true,
  },
];

function fieldLabel(name: string): string {
  return FIELD_LABELS[name] ?? name;
}

function confidenceColor(c: number | null): string {
  if (c == null) return "bg-slate-700 text-slate-300";
  if (c >= 0.8) return "bg-green-900/60 text-green-300";
  if (c >= 0.5) return "bg-amber-900/60 text-amber-300";
  return "bg-red-900/60 text-red-300";
}

function confidenceLabel(c: number | null): string {
  if (c == null) return "—";
  return `${(c * 100).toFixed(0)}%`;
}

interface FieldRowProps {
  field: ExtractionField;
  isActive: boolean;
  disabled: boolean;
  onFocus: () => void;
  onCorrect: (value: string) => void;
}

function FieldRow({
  field,
  isActive,
  disabled,
  onFocus,
  onCorrect,
}: FieldRowProps) {
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState("");
  const isLow = field.confidence != null && field.confidence < 0.6;

  function startEdit() {
    setEditing(true);
    setEditValue(
      field.human_corrected
        ? (field.corrected_value ?? "")
        : (field.field_value ?? ""),
    );
  }

  function submit() {
    if (editValue.trim()) onCorrect(editValue.trim());
    setEditing(false);
  }

  return (
    <div
      className={`px-3 py-2 cursor-pointer transition-colors ${
        isActive
          ? "bg-blue-900/40 border-l-2 border-l-blue-400"
          : isLow
            ? "bg-amber-900/20 border-l-2 border-l-amber-500"
            : "border-l-2 border-l-transparent hover:bg-slate-800/60"
      }`}
      onClick={onFocus}
    >
      <div className="flex items-center justify-between mb-0.5">
        <span className="text-[11px] text-slate-400">
          {fieldLabel(field.field_name)}
        </span>
        <div className="flex items-center gap-1">
          {field.human_corrected && (
            <span className="text-[10px] px-1 py-0.5 bg-blue-800 text-blue-200 rounded">
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

      {editing ? (
        <div className="flex gap-1 mt-1" onClick={(e) => e.stopPropagation()}>
          <input
            autoFocus
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
              if (e.key === "Escape") setEditing(false);
            }}
            className="flex-1 text-sm bg-slate-700 border border-slate-600 text-slate-100 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-400"
          />
          <button
            onClick={submit}
            className="px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600"
          >
            OK
          </button>
        </div>
      ) : (
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-slate-100 break-words max-w-[200px]">
            {field.human_corrected
              ? field.corrected_value
              : (field.field_value ?? "—")}
          </span>
          {!disabled && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                startEdit();
              }}
              className="text-xs text-slate-500 hover:text-blue-400 ml-2 flex-shrink-0"
              style={{ opacity: isActive ? 1 : 0.4 }}
              title="Исправить"
            >
              ✎
            </button>
          )}
        </div>
      )}

      {field.confidence_reason && (
        <span className="text-[10px] text-slate-500 mt-0.5 block">
          {field.confidence_reason}
        </span>
      )}
    </div>
  );
}

export function ExtractionPanel({
  fields,
  overallConfidence,
  activeField,
  onFieldFocus,
  onCorrect,
  disabled = false,
}: ExtractionPanelProps) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({
    bank: true,
    other: true,
  });

  const fieldMap = new Map(fields.map((f) => [f.field_name, f]));

  const knownKeys = new Set(GROUPS.flatMap((g) => g.fields));
  const ungrouped = fields.filter((f) => !knownKeys.has(f.field_name));

  const lowConfCount = fields.filter(
    (f) => f.confidence != null && f.confidence < 0.6,
  ).length;

  return (
    <div className="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-700 bg-slate-800 flex-shrink-0">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-100">
            Извлечённые поля
          </h3>
          {overallConfidence != null && (
            <span
              className={`text-xs px-2 py-0.5 rounded-full ${confidenceColor(overallConfidence)}`}
            >
              {confidenceLabel(overallConfidence)}
            </span>
          )}
        </div>
        {lowConfCount > 0 && (
          <p className="text-xs text-amber-400 mt-1">
            {lowConfCount}{" "}
            {lowConfCount === 1 ? "поле требует" : "полей требуют"} проверки
          </p>
        )}
      </div>

      {/* Groups */}
      <div className="flex-1 overflow-auto">
        {GROUPS.map((group) => {
          const groupFields = group.fields
            .map((name) => fieldMap.get(name))
            .filter(Boolean) as ExtractionField[];

          if (groupFields.length === 0) return null;

          const isCollapsed = collapsed[group.key] ?? false;
          const groupLowCount = groupFields.filter(
            (f) => f.confidence != null && f.confidence < 0.6,
          ).length;

          return (
            <div
              key={group.key}
              className="border-b border-slate-800 last:border-0"
            >
              <button
                className="w-full flex items-center justify-between px-3 py-1.5 bg-slate-800/50 hover:bg-slate-800 transition-colors"
                onClick={() =>
                  group.collapsible &&
                  setCollapsed((prev) => ({
                    ...prev,
                    [group.key]: !isCollapsed,
                  }))
                }
              >
                <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  {group.label}
                  {groupLowCount > 0 && (
                    <span className="ml-1.5 text-amber-400">
                      ({groupLowCount})
                    </span>
                  )}
                </span>
                {group.collapsible && (
                  <span className="text-slate-500 text-xs">
                    {isCollapsed ? "▶" : "▼"}
                  </span>
                )}
              </button>

              {!isCollapsed && (
                <div className="divide-y divide-slate-800/60">
                  {groupFields.map((field) => (
                    <FieldRow
                      key={field.field_name}
                      field={field}
                      isActive={activeField === field.field_name}
                      disabled={disabled}
                      onFocus={() => onFieldFocus(field.field_name)}
                      onCorrect={(v) => onCorrect(field.field_name, v)}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {/* Ungrouped fields (line_N.sku etc) */}
        {ungrouped.length > 0 && (
          <div className="border-b border-slate-800 last:border-0">
            <div className="px-3 py-1.5 bg-slate-800/50">
              <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                Остальное
              </span>
            </div>
            <div className="divide-y divide-slate-800/60">
              {ungrouped.map((field) => (
                <FieldRow
                  key={field.field_name}
                  field={field}
                  isActive={activeField === field.field_name}
                  disabled={disabled}
                  onFocus={() => onFieldFocus(field.field_name)}
                  onCorrect={(v) => onCorrect(field.field_name, v)}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
