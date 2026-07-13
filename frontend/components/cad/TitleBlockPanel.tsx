"use client";

import { useMemo, useState } from "react";

import { CadIr, IrPatchOp } from "@/lib/studio-api";

// ГОСТ 2.104 form-1 fields, in form order. Grouped for a compact layout.
const TEXT_FIELDS: { key: string; label: string; wide?: boolean }[] = [
  { key: "designation", label: "Обозначение", wide: true },
  { key: "name", label: "Наименование", wide: true },
  { key: "material", label: "Материал", wide: true },
  { key: "scale", label: "Масштаб" },
  { key: "litera", label: "Литера" },
  { key: "sheet_no", label: "Лист" },
  { key: "sheet_count", label: "Листов" },
  { key: "developer", label: "Разраб." },
  { key: "checked_by", label: "Пров." },
  { key: "norm_checked_by", label: "Н.контр." },
  { key: "approved_by", label: "Утв." },
  { key: "date", label: "Дата" },
  { key: "company", label: "Предприятие", wide: true },
];

/** C3: structured основная надпись (ГОСТ 2.104) editor. Fills the fields and
 * commits them with one set_title_block op, which re-renders the stamp text
 * (and draws the frame when the sheet has none). */
export default function TitleBlockPanel({
  ir,
  busy,
  onApply,
  t,
}: {
  ir: CadIr;
  busy: boolean;
  onApply: (ops: IrPatchOp[]) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const stored = useMemo(() => {
    const tb = (ir.sheet.title_block ?? {}) as Record<string, unknown>;
    const fields = (tb.fields ?? {}) as Record<string, unknown>;
    return fields;
  }, [ir]);

  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      TEXT_FIELDS.map((f) => [
        f.key,
        stored[f.key] != null ? String(stored[f.key]) : "",
      ]),
    ),
  );
  const [mass, setMass] = useState(
    stored.mass_kg != null ? String(stored.mass_kg) : "",
  );

  function submit() {
    const payload: Record<string, string | number> = {};
    for (const f of TEXT_FIELDS) {
      const v = values[f.key]?.trim();
      if (v) payload[f.key] = v;
    }
    if (mass.trim() && Number.isFinite(Number(mass))) {
      payload.mass_kg = Number(mass);
    }
    onApply([{ op: "set_title_block", title_block: payload }]);
  }

  return (
    <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-xs text-zinc-300"
      >
        <span>{t("vector.title_block")}</span>
        <span className="text-zinc-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-1.5">
            {TEXT_FIELDS.map((f) => (
              <label
                key={f.key}
                className={`flex flex-col gap-0.5 text-[10px] text-zinc-500 ${f.wide ? "col-span-2" : ""}`}
              >
                {f.label}
                <input
                  value={values[f.key] ?? ""}
                  onChange={(e) =>
                    setValues((cur) => ({ ...cur, [f.key]: e.target.value }))
                  }
                  className="rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
                />
              </label>
            ))}
            <label className="flex flex-col gap-0.5 text-[10px] text-zinc-500">
              {t("vector.title_mass")}
              <input
                value={mass}
                inputMode="decimal"
                onChange={(e) => setMass(e.target.value)}
                className="rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
              />
            </label>
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={submit}
            className="w-full rounded bg-sky-600 px-3 py-1.5 text-xs text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("vector.title_apply")}
          </button>
        </div>
      )}
    </div>
  );
}
