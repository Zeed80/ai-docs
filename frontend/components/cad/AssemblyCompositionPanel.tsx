"use client";

/** Состав сборки, прочитанный со сборочного чертежа (план, X5/Ф6).
 *
 * 3D сборки по чертежу не строится — оператор получает то, ради чего
 * сборочный чертёж читают: какие позиции на нём показаны и что каждая значит
 * по спецификации. Несвязанное не прячется: позиция без строки и строка без
 * позиции — отдельными строками, это и есть то, что проверять.
 */

export type AssemblyComposition = {
  positions: number[];
  rows: number;
  linked: {
    position: number;
    designation?: string | null;
    name?: string | null;
    quantity?: number | null;
  }[];
  positions_without_row: number[];
  rows_without_position: number[];
  duplicated_rows: number[];
  link_rate: number;
  specification_source?: string;
};

export default function AssemblyCompositionPanel({
  assembly,
  t,
}: {
  assembly?: AssemblyComposition;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  if (!assembly) return null;
  const issues: string[] = [];
  if (assembly.positions_without_row.length)
    issues.push(
      t("vector.assembly_positions_without_row", {
        list: assembly.positions_without_row.join(", "),
      }),
    );
  if (assembly.rows_without_position.length)
    issues.push(
      t("vector.assembly_rows_without_position", {
        list: assembly.rows_without_position.join(", "),
      }),
    );
  if (assembly.duplicated_rows.length)
    issues.push(
      t("vector.assembly_duplicated_rows", {
        list: assembly.duplicated_rows.join(", "),
      }),
    );
  return (
    <section
      className="rounded border border-white/10 bg-zinc-950/60 p-3 text-xs"
      aria-labelledby="assembly-composition-title"
    >
      <h3
        id="assembly-composition-title"
        className="mb-1 text-sm font-medium text-zinc-200"
      >
        {t("vector.assembly_title")}
      </h3>
      <p className="mb-2 text-zinc-400">
        {t("vector.assembly_summary", {
          positions: assembly.positions.length,
          rows: assembly.rows,
          linked: assembly.linked.length,
        })}
      </p>
      {issues.length ? (
        <ul className="mb-2 space-y-1 text-amber-200">
          {issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      ) : null}
      {assembly.linked.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[20rem] text-left text-zinc-300">
            <thead className="text-zinc-500">
              <tr>
                <th className="pr-2 font-normal">
                  {t("vector.assembly_col_position")}
                </th>
                <th className="pr-2 font-normal">
                  {t("vector.assembly_col_designation")}
                </th>
                <th className="pr-2 font-normal">
                  {t("vector.assembly_col_name")}
                </th>
                <th className="font-normal">
                  {t("vector.assembly_col_quantity")}
                </th>
              </tr>
            </thead>
            <tbody>
              {assembly.linked.map((row) => (
                <tr key={row.position} className="border-t border-white/5">
                  <td className="pr-2">{row.position}</td>
                  <td className="break-all pr-2">{row.designation ?? "—"}</td>
                  <td className="pr-2">{row.name ?? "—"}</td>
                  <td>{row.quantity ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
