"use client";

import { useEffect, useMemo, useState } from "react";

import { engineeringApi } from "@/lib/engineering-api";
import type {
  EmgFeatureNode,
  EmgOperationNode,
  EmgParam,
} from "@/lib/emg-tree";
import { isLikelyMirrorable, kindLabel } from "@/lib/emg-tree";

const ORIGIN_LABEL: Record<string, string> = {
  observed: "Наблюдение",
  traced: "Трассировка",
  derived: "Выведено",
  standard: "Стандарт",
  assumed: "Предположение",
  human: "Инженер",
};

function originClasses(origin: string): string {
  if (origin === "assumed") return "bg-amber-500/15 text-amber-300";
  if (origin === "human") return "bg-emerald-500/15 text-emerald-300";
  if (origin === "observed" || origin === "standard")
    return "bg-sky-500/15 text-sky-300";
  return "bg-white/5 text-zinc-400";
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return String(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** One editable field bound to a Feature's own assertion — the ONLY kind of
 * correction that reliably survives a rebuild (feature_spec_path mirrors it
 * onto the same spec leaf feature_tree_from_spec reads again; see cad_emg_
 * compat.py). A raw operation.param.* edit with no source Feature is shown
 * read-only, honestly, rather than offered as an input that would silently
 * be discarded on the next rebuild. */
function FeatureParamField({
  param,
  draft,
  onChange,
}: {
  param: EmgParam;
  draft: string;
  onChange: (value: string) => void;
}) {
  const isNumeric = typeof param.value === "number";
  return (
    <label className="block space-y-1">
      <span className="flex items-center gap-2 text-[11px] text-zinc-400">
        <span className="font-mono">{param.name}</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] ${originClasses(param.origin)}`}
        >
          {ORIGIN_LABEL[param.origin] ?? param.origin}
        </span>
      </span>
      <input
        value={draft}
        onChange={(event) => onChange(event.target.value)}
        inputMode={isNumeric ? "decimal" : "text"}
        className="w-full rounded border border-white/10 bg-black/30 px-2 py-1.5 font-mono text-[12px] text-zinc-100 outline-none focus:border-sky-400/60"
      />
    </label>
  );
}

function ReadOnlyParamRow({ param }: { param: EmgParam }) {
  return (
    <div className="flex items-center justify-between gap-2 py-1 text-[11px]">
      <span className="font-mono text-zinc-400">{param.name}</span>
      <span className="flex items-center gap-1.5">
        <span className="font-mono text-zinc-200">
          {formatValue(param.value)}
        </span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] ${originClasses(param.origin)}`}
        >
          {ORIGIN_LABEL[param.origin] ?? param.origin}
        </span>
      </span>
    </div>
  );
}

export default function PropertiesPanel({
  generationId,
  operation,
  features,
  onSaved,
  onRebuildQueued,
  onError,
}: {
  generationId: string;
  operation: EmgOperationNode | null;
  features: EmgFeatureNode[];
  onSaved: () => void;
  onRebuildQueued: (taskId: string) => void;
  onError: (message: string) => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const editableParams = useMemo(
    () =>
      features.flatMap((feature) =>
        feature.params
          .filter((param) => isLikelyMirrorable(feature.id, param.predicate))
          .map((param) => ({ feature, param })),
      ),
    [features],
  );

  useEffect(() => {
    setDrafts(
      Object.fromEntries(
        editableParams.map(({ param }) => [
          param.assertionId,
          formatValue(param.value),
        ]),
      ),
    );
    setMessage(null);
  }, [editableParams]);

  if (!operation) {
    return (
      <p className="p-3 text-xs text-zinc-400">
        Выберите элемент в дереве слева, чтобы увидеть и поправить его значения.
      </p>
    );
  }

  const changed = editableParams.filter(
    ({ param }) => drafts[param.assertionId] !== formatValue(param.value),
  );

  async function submit(rebuild: boolean) {
    if (!note.trim()) {
      onError("Опишите инженерное обоснование правки перед сохранением.");
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const corrections = editableParams.map(({ param }) => {
        const raw = drafts[param.assertionId] ?? formatValue(param.value);
        const numeric = Number(raw.replace(",", "."));
        const value =
          typeof param.value === "number" && Number.isFinite(numeric)
            ? numeric
            : raw;
        return {
          assertion_id: param.assertionId,
          value: { kind: "exact" as const, value },
        };
      });
      const result = await engineeringApi.correctGenerationAssertionsBatch(
        generationId,
        {
          corrections,
          note: note.trim(),
          idempotency_key: `editor:${crypto.randomUUID()}`,
          rebuild,
        },
      );
      const local = result.dependency_validation;
      const affectedCount = new Set([
        ...local.affected_build_operation_ids,
        ...local.affected_topology_element_ids,
        ...local.affected_artifact_ids,
      ]).size;
      setMessage(
        local.status === "blocked"
          ? `Сохранено, но dependency-проверка заблокирована: ${local.validation_errors.join(", ")}`
          : rebuild && result.rebuild_task_id
            ? `Локально проверено ${affectedCount} зависимых узлов; геометрическая пересборка поставлена в очередь`
            : local.requires_kernel_rebuild
              ? `Локально проверено ${affectedCount} зависимых узлов; геометрия требует пересборки`
              : `Локально проверено ${affectedCount} зависимых узлов; геометрическая пересборка не требуется`,
      );
      if (result.rebuild_task_id) onRebuildQueued(result.rebuild_task_id);
      setNote("");
      onSaved();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  const readOnlyOperationParams = operation.params;

  return (
    <div className="space-y-4 p-3 text-xs">
      <div>
        <p className="text-sm font-medium text-zinc-100">
          {kindLabel(operation.kind)}
        </p>
        <p className="mt-0.5 font-mono text-[10px] text-zinc-400">
          {operation.id}
        </p>
      </div>

      {readOnlyOperationParams.length > 0 && (
        <section>
          <h4 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
            Параметры операции (что реально построено)
          </h4>
          <div className="divide-y divide-white/5 rounded border border-white/10 bg-black/20 px-2">
            {readOnlyOperationParams.map((param) => (
              <ReadOnlyParamRow key={param.assertionId} param={param} />
            ))}
          </div>
        </section>
      )}

      {features.length === 0 && (
        <p className="rounded border border-white/10 bg-white/5 p-2 text-[11px] text-zinc-400">
          Эта операция не связана ни с одним элементом чтения (например, базовая
          форма из всего профиля, или фича, добавленная вручную) — править здесь
          нечего; значения выше показаны для справки.
        </p>
      )}

      {features.map((feature) => {
        const params = feature.params.filter((param) =>
          isLikelyMirrorable(feature.id, param.predicate),
        );
        if (params.length === 0 && !feature.location) return null;
        return (
          <section key={feature.id}>
            <h4 className="mb-1 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
              Прочитано с чертежа — {kindLabel(feature.kind)}
              <span className="font-mono normal-case text-zinc-400">
                {feature.id}
              </span>
            </h4>
            <div className="space-y-2 rounded border border-white/10 bg-black/20 p-2">
              {params.map((param) => (
                <FeatureParamField
                  key={param.assertionId}
                  param={param}
                  draft={drafts[param.assertionId] ?? formatValue(param.value)}
                  onChange={(value) =>
                    setDrafts((current) => ({
                      ...current,
                      [param.assertionId]: value,
                    }))
                  }
                />
              ))}
            </div>
          </section>
        );
      })}

      {editableParams.length > 0 && (
        <section className="space-y-2 border-t border-white/10 pt-3">
          <label className="block space-y-1">
            <span className="text-[11px] text-zinc-400">
              Инженерное обоснование
            </span>
            <textarea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Например: длина ступени уточнена по оригиналу"
              className="h-16 w-full rounded border border-white/10 bg-black/30 p-2 text-[11px] text-zinc-100 outline-none focus:border-sky-400/60"
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void submit(true)}
              className="rounded bg-emerald-500/20 px-3 py-1.5 text-emerald-200 hover:bg-emerald-500/30 disabled:opacity-40"
            >
              {changed.length > 0
                ? `Сохранить и пересобрать (${changed.length})`
                : "Подтвердить как есть и пересобрать"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void submit(false)}
              className="rounded border border-white/15 px-3 py-1.5 text-zinc-300 hover:bg-white/5 disabled:opacity-40"
            >
              Сохранить без пересборки
            </button>
          </div>
          {message && <p className="text-emerald-300">{message}</p>}
        </section>
      )}
    </div>
  );
}
