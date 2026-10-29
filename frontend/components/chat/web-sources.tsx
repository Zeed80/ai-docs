"use client";

/**
 * W6: what the employee read on the web, visible in the chat.
 *
 * - `CitedText` turns the answer's `[n]` marks into footnotes: hover shows the
 *   page, click opens it. The pages come from the message (`web_sources` in
 *   its metadata, or the live `web.sources` event).
 * - `WebToolDetails` opens a search / browser tool card: which queries, which
 *   pages with their kind and read status, and for a deep research the table
 *   of facts with their agreement.
 */

import type { ReactNode } from "react";

export interface WebSource {
  n: number;
  url: string;
  title?: string | null;
  domain?: string | null;
  kind?: string | null;
  read_status?: string | null;
}

export function asWebSources(raw: unknown): WebSource[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out = raw
    .filter(
      (s): s is Record<string, unknown> =>
        !!s && typeof s === "object" && typeof (s as { url?: unknown }).url === "string",
    )
    .map((s) => ({
      n: Number(s.n),
      url: String(s.url),
      title: typeof s.title === "string" ? s.title : null,
      domain: typeof s.domain === "string" ? s.domain : null,
      kind: typeof s.kind === "string" ? s.kind : null,
      read_status: typeof s.read_status === "string" ? s.read_status : null,
    }))
    .filter((s) => Number.isFinite(s.n) && /^https?:\/\//.test(s.url));
  return out.length ? out : undefined;
}

export const KIND_LABEL: Record<string, string> = {
  manufacturer: "производитель",
  official_distributor: "официальный дилер",
  distributor: "дистрибьютор",
  marketplace: "магазин",
  catalog: "каталог",
  directory: "справочник компаний",
  forum: "форум",
  news: "новости",
  reference: "справочник",
  unknown: "источник",
};

const STATUS_LABEL: Record<string, string> = {
  ok: "прочитан",
  blocked: "проверка на бота",
  empty: "пустая страница",
  error: "не открылся",
};

const AGREEMENT: Record<string, { label: string; className: string }> = {
  confirmed: { label: "подтверждено", className: "text-emerald-300" },
  single: { label: "один источник", className: "text-amber-300" },
  conflict: { label: "расхождение", className: "text-red-300" },
  missing: { label: "не найдено", className: "text-slate-400" },
};

const CITE_RE = /\[(\d{1,3})\]/g;

/** Plain text with `[n]` marks as footnote links (unknown numbers stay text). */
export function citeText(text: string, sources: WebSource[] | undefined, keyBase: number): ReactNode[] {
  if (!sources?.length) return [text];
  const byN = new Map(sources.map((s) => [s.n, s]));
  const out: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(CITE_RE)) {
    const source = byN.get(Number(match[1]));
    const start = match.index ?? 0;
    if (!source) continue;
    if (start > last) out.push(text.slice(last, start));
    const name = source.title || source.domain || source.url;
    out.push(
      <a
        key={`${keyBase}-c${start}`}
        href={source.url}
        target="_blank"
        rel="noopener noreferrer"
        title={`${name}${source.kind ? ` · ${KIND_LABEL[source.kind] ?? source.kind}` : ""}\n${source.url}`}
        aria-label={`Источник ${source.n}: ${name}`}
        className="mx-0.5 align-super text-[10px] font-semibold text-sky-300 no-underline hover:text-sky-200"
      >
        [{source.n}]
      </a>,
    );
    last = start + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Obj = Record<string, unknown>;

function payload(result: unknown): Obj | null {
  if (!result || typeof result !== "object") return null;
  const r = result as Obj;
  if (r.version === 1 && r.data && typeof r.data === "object") return r.data as Obj;
  if (r.result && typeof r.result === "object" && "action_id" in r) return r.result as Obj;
  return r;
}

function domainOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Whether a tool card has web details to open. */
export function hasWebDetails(tool: string | undefined, result: unknown): boolean {
  if (!tool || !/^(search|computer_use)/.test(tool)) return false;
  const data = payload(result);
  return !!data && (Array.isArray(data.sources) || Array.isArray(data.results) || typeof data.url === "string");
}

export function WebToolDetails({ result }: { result: unknown }) {
  const data = payload(result);
  if (!data) return null;
  const sources = (Array.isArray(data.sources) ? data.sources : []) as Obj[];
  const results = (Array.isArray(data.results) ? data.results : []) as Obj[];
  const facts = (Array.isArray(data.facts) ? data.facts : []) as Obj[];
  const queries = (Array.isArray(data.queries) ? data.queries : []) as string[];
  const single =
    !sources.length && !results.length && typeof data.url === "string" ? [data] : [];
  return (
    <div className="mt-1 space-y-2 rounded-md border border-slate-700 bg-slate-900/60 p-2 text-[11px] text-slate-300 min-w-0">
      {queries.length > 0 && (
        <p className="break-words text-slate-400">
          Запросы: {queries.slice(0, 8).join(" · ")}
        </p>
      )}
      {facts.length > 0 && (
        <table className="w-full table-fixed text-left">
          <tbody>
            {facts.map((fact, i) => {
              const values = (Array.isArray(fact.values) ? fact.values : []) as Obj[];
              const agreement = AGREEMENT[String(fact.status)] ?? AGREEMENT.missing;
              return (
                <tr key={i} className="align-top border-t border-slate-800">
                  <td className="w-1/3 break-words pr-2">{String(fact.name)}</td>
                  <td className="break-words pr-2">
                    {values.length
                      ? values
                          .map(
                            (v) =>
                              `${String(v.value)}${v.unit && !String(v.value).includes(String(v.unit)) ? ` ${String(v.unit)}` : ""} ${((v.sources as number[]) || []).map((n) => `[${n}]`).join("")}`,
                          )
                          .join("; ")
                      : "—"}
                  </td>
                  <td className={`w-24 ${agreement.className}`}>{agreement.label}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {[...sources, ...single].length > 0 && (
        <ol className="space-y-1">
          {[...sources, ...single].map((s, i) => {
            const url = String(s.url ?? s.final_url ?? "");
            const status = String(s.read_status ?? "ok");
            return (
              <li key={i} className="flex min-w-0 flex-wrap items-baseline gap-x-1.5">
                <span className="font-mono text-slate-500">
                  {s.cite_as ? String(s.cite_as) : `${i + 1}.`}
                </span>
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="min-w-0 max-w-full truncate text-sky-300 hover:underline"
                  title={url}
                >
                  {String(s.title || domainOf(url))}
                </a>
                <span className="text-slate-500">{domainOf(url)}</span>
                {typeof s.kind === "string" && (
                  <span className="rounded bg-slate-800 px-1 text-slate-300">
                    {KIND_LABEL[s.kind] ?? s.kind}
                  </span>
                )}
                {s.trusted === true && (
                  <span className="rounded bg-emerald-900/50 px-1 text-emerald-300">в реестре</span>
                )}
                <span className={status === "ok" ? "text-emerald-400" : "text-amber-300"}>
                  {STATUS_LABEL[status] ?? status}
                  {s.challenge ? ` (${String(s.challenge)})` : ""}
                </span>
                {typeof s.duplicate_of === "number" && (
                  <span className="text-slate-500">копия [{s.duplicate_of}]</span>
                )}
              </li>
            );
          })}
        </ol>
      )}
      {results.length > 0 && (
        <ol className="space-y-0.5">
          {results.slice(0, 10).map((r, i) => (
            <li key={i} className="min-w-0 truncate">
              <a
                href={String(r.url)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sky-300 hover:underline"
                title={String(r.url)}
              >
                {String(r.title || r.url)}
              </a>{" "}
              <span className="text-slate-500">{domainOf(String(r.url))}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
