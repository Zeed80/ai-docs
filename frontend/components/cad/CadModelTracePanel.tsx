"use client";

export type KernelInput = {
  sha256?: string;
  payload?: {
    candidate?: {
      label?: string;
      features?: Array<{
        kind?: string;
        params?: Record<string, unknown>;
        param_provenance?: Record<string, unknown>;
      }>;
      missing_data?: string[];
    };
    confirm_assumptions?: boolean;
    metadata?: Record<string, unknown>;
  };
};

type SolidAudit = {
  build_status?: string;
  blockers?: string[];
  warnings?: string[];
  build_gate?: { blockers?: string[]; warnings?: string[] };
};

export type ModelEvidence = {
  pass?: number;
  image_index?: number;
  bbox?: number[];
  source_bbox?: number[];
  raw_text?: string;
};

type ValueProvenance = {
  value?: unknown;
  votes?: number;
  passes?: number;
  confidence?: number;
  evidence?: ModelEvidence[];
};

/** Human-readable audit boundary: model reading -> exact CAD-kernel request. */
export default function CadModelTracePanel({
  reading,
  kernelInput,
  solid,
  onEvidenceFocus,
  t,
}: {
  reading?: Record<string, unknown>;
  kernelInput?: KernelInput;
  solid?: SolidAudit;
  onEvidenceFocus?: (evidence: ModelEvidence) => void;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  if (!reading && !kernelInput) return null;
  const candidate = kernelInput?.payload?.candidate;
  const blockers = solid?.build_gate?.blockers ?? solid?.blockers ?? [];
  const warnings = solid?.build_gate?.warnings ?? solid?.warnings ?? [];
  const spec = (reading?.spec ?? reading) as Record<string, unknown> | undefined;
  const provenance = Object.entries(
    (spec?.value_provenance as Record<string, ValueProvenance> | undefined) ?? {},
  );

  return (
    <section className="rounded border border-sky-400/20 bg-sky-950/10 p-3 text-xs">
      <h3 className="text-sm font-medium text-sky-100">
        {t("vector.model_trace_title")}
      </h3>
      <p className="mt-1 text-[11px] text-zinc-500">
        {t("vector.model_trace_hint")}
      </p>

      {(blockers.length > 0 || warnings.length > 0) && (
        <div className="mt-2 space-y-1">
          {blockers.map((item) => (
            <div key={`block-${item}`} className="text-red-300">
              ✕ {item}
            </div>
          ))}
          {warnings.map((item) => (
            <div key={`warn-${item}`} className="text-amber-300">
              • {item}
            </div>
          ))}
        </div>
      )}

      {provenance.length > 0 && (
        <details className="mt-3 rounded border border-white/10 bg-black/20 p-2" open>
          <summary className="cursor-pointer font-medium text-zinc-200">
            {t("vector.model_trace_values")}
          </summary>
          <div className="mt-2 max-h-80 overflow-auto">
            <table className="w-full text-left text-[10px]">
              <thead className="sticky top-0 bg-zinc-950 text-zinc-500">
                <tr>
                  <th className="p-1">{t("vector.model_trace_path")}</th>
                  <th className="p-1">{t("vector.model_trace_value")}</th>
                  <th className="p-1">{t("vector.model_trace_votes")}</th>
                  <th className="p-1">{t("vector.model_trace_evidence")}</th>
                </tr>
              </thead>
              <tbody>
                {provenance.map(([path, item]) => {
                  const evidence = (item.evidence ?? []).find(
                    (entry) => entry.source_bbox?.length === 4,
                  );
                  return (
                    <tr key={path} className="border-t border-white/5 text-zinc-300">
                      <td className="p-1 font-mono text-zinc-400">{path}</td>
                      <td className="p-1 font-mono">{String(item.value ?? "—")}</td>
                      <td className="p-1 whitespace-nowrap">
                        {item.votes ?? 0}/{item.passes ?? 0} · {Math.round((item.confidence ?? 0) * 100)}%
                      </td>
                      <td className="p-1">
                        {evidence ? (
                          <button
                            type="button"
                            onClick={() => onEvidenceFocus?.(evidence)}
                            className="rounded bg-sky-600/30 px-1.5 py-0.5 text-sky-200 hover:bg-sky-600/50"
                            title={evidence.raw_text}
                          >
                            {t("vector.model_trace_show_source")}
                          </button>
                        ) : (
                          <span className="text-zinc-600">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </details>
      )}

      <div className="mt-3 grid gap-2 lg:grid-cols-2">
        <details className="rounded border border-white/10 bg-black/20 p-2" open>
          <summary className="cursor-pointer font-medium text-zinc-200">
            {t("vector.model_trace_reading")}
          </summary>
          <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] text-zinc-400">
            {JSON.stringify(reading, null, 2)}
          </pre>
        </details>

        <details className="rounded border border-white/10 bg-black/20 p-2" open>
          <summary className="cursor-pointer font-medium text-zinc-200">
            {t("vector.model_trace_kernel_input")}
          </summary>
          <div className="mt-2 text-[11px] text-zinc-400">
            {t("vector.model_trace_status")}: {solid?.build_status ?? "blocked"}
          </div>
          <div className="mt-1 break-all font-mono text-[10px] text-zinc-500">
            SHA-256: {kernelInput?.sha256 ?? "—"}
          </div>
          <div className="mt-2 space-y-2">
            {(candidate?.features ?? []).map((feature, index) => (
              <div
                key={`${feature.kind ?? "feature"}-${index}`}
                className="rounded border border-white/5 p-2"
              >
                <div className="font-mono text-sky-300">
                  {index + 1}. {feature.kind ?? "—"}
                </div>
                <pre className="mt-1 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] text-zinc-400">
                  {JSON.stringify(feature.params ?? {}, null, 2)}
                </pre>
                {feature.param_provenance && (
                  <pre className="mt-1 overflow-auto whitespace-pre-wrap break-words border-t border-white/5 pt-1 font-mono text-[10px] text-zinc-500">
                    {JSON.stringify(feature.param_provenance, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </div>
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-zinc-400">
              {t("vector.model_trace_raw_payload")}
            </summary>
            <pre className="mt-1 max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] text-zinc-500">
              {JSON.stringify(kernelInput?.payload, null, 2)}
            </pre>
          </details>
        </details>
      </div>
    </section>
  );
}
