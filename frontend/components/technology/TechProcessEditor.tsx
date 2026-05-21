"use client";

import { useState } from "react";

export interface Operation {
  id: string;
  sequence_no: number;
  operation_code: string | null;
  gost_operation_code: string | null;
  name: string;
  operation_type: string | null;
  setup_description: string | null;
  transition_text: string | null;
  control_requirements: string | null;
  cutting_parameters: Record<string, unknown> | null;
  machine_resource_id: string | null;
  to_minutes: number | null;
  tv_minutes: number | null;
  tsht_minutes: number | null;
  tsht_k_minutes: number | null;
  tpz_minutes: number | null;
}

interface Resource {
  id: string;
  name: string;
  code: string | null;
  model: string | null;
}

interface Props {
  operations: Operation[];
  resources?: Resource[];
  selectedOperationId?: string | null;
  onSelectOperation?: (op: Operation) => void;
  onUpdateOperation?: (
    id: string,
    field: string,
    value: unknown,
  ) => Promise<void>;
}

const OP_TYPE_LABELS: Record<string, string> = {
  blank_preparation: "Заготовительная",
  turning: "Токарная",
  milling: "Фрезерная",
  drilling: "Сверлильная",
  boring: "Расточная",
  reaming: "Развёрточная",
  grinding: "Шлифовальная",
  honing: "Хонинговальная",
  broaching: "Протяжная",
  quality_control: "Контроль",
  assembly: "Сборочная",
  heat_treatment: "Термическая",
  other: "Прочая",
};

const OP_TYPE_COLOR: Record<string, string> = {
  blank_preparation: "bg-zinc-700 text-zinc-300",
  turning: "bg-blue-900/60 text-blue-300",
  milling: "bg-violet-900/60 text-violet-300",
  drilling: "bg-cyan-900/60 text-cyan-300",
  boring: "bg-sky-900/60 text-sky-300",
  grinding: "bg-amber-900/60 text-amber-300",
  quality_control: "bg-emerald-900/60 text-emerald-300",
  assembly: "bg-teal-900/60 text-teal-300",
  heat_treatment: "bg-orange-900/60 text-orange-300",
  other: "bg-zinc-700 text-zinc-400",
};

function CellInput({
  value,
  onSave,
  mono = false,
}: {
  value: string | number | null;
  onSave: (v: string) => void;
  mono?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(value ?? ""));

  if (!editing) {
    return (
      <span
        className={`cursor-pointer hover:underline hover:decoration-dotted ${mono ? "font-mono" : ""}`}
        onClick={() => {
          setDraft(String(value ?? ""));
          setEditing(true);
        }}
      >
        {value ?? <span className="text-zinc-600 italic">—</span>}
      </span>
    );
  }

  return (
    <input
      autoFocus
      className={`w-full bg-zinc-900 border border-blue-500 rounded px-1 py-0.5 text-xs outline-none ${mono ? "font-mono" : ""}`}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        onSave(draft);
        setEditing(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          onSave(draft);
          setEditing(false);
        }
        if (e.key === "Escape") setEditing(false);
      }}
    />
  );
}

export default function TechProcessEditor({
  operations,
  resources = [],
  selectedOperationId,
  onSelectOperation,
  onUpdateOperation,
}: Props) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const sorted = [...operations].sort((a, b) => a.sequence_no - b.sequence_no);

  const handleUpdate = async (id: string, field: string, value: unknown) => {
    if (onUpdateOperation) {
      await onUpdateOperation(id, field, value);
    }
  };

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="bg-zinc-800 text-zinc-400 sticky top-0 z-10">
            <th className="px-2 py-2 text-left w-12">№</th>
            <th className="px-2 py-2 text-left w-16">Код</th>
            <th className="px-2 py-2 text-left min-w-[140px]">Операция</th>
            <th className="px-2 py-2 text-left w-20">Тип</th>
            <th className="px-2 py-2 text-right w-14">То</th>
            <th className="px-2 py-2 text-right w-14">Тшт</th>
            <th className="px-2 py-2 text-right w-16">Тшт-к</th>
            <th className="px-2 py-2 w-8"></th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((op) => {
            const isSelected = op.id === selectedOperationId;
            const isExpanded = op.id === expandedId;
            const typeLabel =
              OP_TYPE_LABELS[op.operation_type ?? ""] ??
              op.operation_type ??
              "";
            const typeColor =
              OP_TYPE_COLOR[op.operation_type ?? ""] ??
              "bg-zinc-700 text-zinc-400";

            return (
              <>
                <tr
                  key={op.id}
                  onClick={() => onSelectOperation?.(op)}
                  className={`border-b border-zinc-800 transition cursor-pointer
                    ${isSelected ? "bg-blue-900/25 border-l-2 border-l-blue-500" : "hover:bg-zinc-800/40"}
                  `}
                >
                  <td className="px-2 py-1.5 font-mono text-zinc-300">
                    {String(op.sequence_no).padStart(3, "0")}
                  </td>
                  <td className="px-2 py-1.5">
                    {onUpdateOperation ? (
                      <CellInput
                        value={op.operation_code}
                        mono
                        onSave={(v) => handleUpdate(op.id, "operation_code", v)}
                      />
                    ) : (
                      <span className="font-mono text-zinc-400">
                        {op.operation_code ?? "—"}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-zinc-200 font-medium">
                    {onUpdateOperation ? (
                      <CellInput
                        value={op.name}
                        onSave={(v) => handleUpdate(op.id, "name", v)}
                      />
                    ) : (
                      op.name
                    )}
                  </td>
                  <td className="px-2 py-1.5">
                    <span
                      className={`text-xs px-1.5 py-0.5 rounded ${typeColor}`}
                    >
                      {typeLabel}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-zinc-400">
                    {op.to_minutes != null ? op.to_minutes.toFixed(2) : "—"}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-zinc-400">
                    {op.tsht_minutes != null ? op.tsht_minutes.toFixed(2) : "—"}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-zinc-200 font-medium">
                    {op.tsht_k_minutes != null
                      ? op.tsht_k_minutes.toFixed(2)
                      : "—"}
                  </td>
                  <td className="px-2 py-1.5 text-center">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setExpandedId(isExpanded ? null : op.id);
                      }}
                      className="text-zinc-500 hover:text-zinc-300 transition"
                    >
                      {isExpanded ? "▲" : "▼"}
                    </button>
                  </td>
                </tr>

                {isExpanded && (
                  <tr key={`${op.id}-detail`} className="bg-zinc-900/60">
                    <td colSpan={8} className="px-4 py-3">
                      <div className="grid grid-cols-2 gap-4 text-xs">
                        <div>
                          <p className="text-zinc-500 mb-0.5">
                            Установка / описание:
                          </p>
                          <p className="text-zinc-300 leading-relaxed">
                            {op.setup_description || "—"}
                          </p>
                        </div>
                        <div>
                          <p className="text-zinc-500 mb-0.5">Переходы:</p>
                          <p className="text-zinc-300 leading-relaxed whitespace-pre-line">
                            {op.transition_text || "—"}
                          </p>
                        </div>
                        <div>
                          <p className="text-zinc-500 mb-0.5">
                            Требования к контролю:
                          </p>
                          <p className="text-zinc-300">
                            {op.control_requirements || "—"}
                          </p>
                        </div>
                        {op.cutting_parameters && (
                          <div>
                            <p className="text-zinc-500 mb-0.5">
                              Режимы резания:
                            </p>
                            <div className="font-mono text-zinc-300 space-y-0.5">
                              {Object.entries(
                                op.cutting_parameters as Record<
                                  string,
                                  unknown
                                >,
                              )
                                .filter(([k]) =>
                                  [
                                    "vc_m_min",
                                    "n_rpm",
                                    "feed_mm_min",
                                    "ap_mm",
                                  ].includes(k),
                                )
                                .map(([k, v]) => (
                                  <div key={k}>
                                    <span className="text-zinc-500">{k}=</span>
                                    <span>{String(v)}</span>
                                  </div>
                                ))}
                            </div>
                          </div>
                        )}
                        <div className="col-span-2">
                          <p className="text-zinc-500 mb-0.5">
                            Нормы времени (мин):
                          </p>
                          <div className="flex gap-4 font-mono text-zinc-300">
                            {[
                              ["То", op.to_minutes],
                              ["Тв", op.tv_minutes],
                              ["Тшт", op.tsht_minutes],
                              ["Тшт-к", op.tsht_k_minutes],
                              ["Тпз", op.tpz_minutes],
                            ].map(([label, val]) => (
                              <span key={String(label)}>
                                <span className="text-zinc-500">{label}=</span>
                                {val != null ? Number(val).toFixed(2) : "—"}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </>
            );
          })}
        </tbody>
        {operations.length > 0 && (
          <tfoot>
            <tr className="bg-zinc-800 font-semibold">
              <td colSpan={4} className="px-2 py-1.5 text-zinc-400 text-xs">
                Итого ({operations.length} опер.)
              </td>
              <td className="px-2 py-1.5 text-right font-mono text-zinc-300 text-xs">
                {operations
                  .reduce((s, o) => s + (o.to_minutes ?? 0), 0)
                  .toFixed(2)}
              </td>
              <td className="px-2 py-1.5 text-right font-mono text-zinc-300 text-xs">
                {operations
                  .reduce((s, o) => s + (o.tsht_minutes ?? 0), 0)
                  .toFixed(2)}
              </td>
              <td className="px-2 py-1.5 text-right font-mono text-zinc-200 text-xs">
                {operations
                  .reduce((s, o) => s + (o.tsht_k_minutes ?? 0), 0)
                  .toFixed(2)}
              </td>
              <td></td>
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}
