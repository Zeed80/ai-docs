"use client";

/**
 * Renders the cooling setup instruction inside the settings page.
 *
 * The text comes from the repository's own docs/cooling-motherboard-fans.md,
 * served by GET /api/cooling/setup-guide, so there is exactly one copy of the
 * instruction and it cannot drift from what the repo ships.
 *
 * The formatter below deliberately covers only the constructs that document
 * uses — headings, paragraphs, lists, fenced code, tables, blockquotes and
 * inline code/bold/links. It is not a general Markdown implementation and
 * should not be reused as one.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch } from "@/lib/auth";

const BASE = `${getApiBaseUrl()}/api/cooling`;

/** Inline: `code`, **bold**, [text](url). */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = pattern.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const token = m[0];
    const key = `${keyPrefix}-i${i++}`;
    if (token.startsWith("`")) {
      out.push(
        <code
          key={key}
          className="px-1 py-0.5 rounded bg-muted font-mono text-[0.9em]"
        >
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("**")) {
      out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else {
      const split = token.indexOf("](");
      const label = token.slice(1, split);
      const href = token.slice(split + 2, -1);
      out.push(
        <a
          key={key}
          href={href}
          target="_blank"
          rel="noreferrer"
          className="text-blue-500 hover:underline"
        >
          {label}
        </a>,
      );
    }
    last = m.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function tableRow(line: string): string[] {
  return line
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());
}

function render(markdown: string): ReactNode[] {
  const lines = markdown.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  const paragraph: string[] = [];
  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    const text = paragraph.join(" ");
    paragraph.length = 0;
    blocks.push(
      <p key={`p${key++}`} className="text-sm leading-relaxed">
        {inline(text, `p${key}`)}
      </p>,
    );
  };

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith("```")) {
      flushParagraph();
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```"))
        code.push(lines[i++]);
      i++; // closing fence
      blocks.push(
        <pre
          key={`c${key++}`}
          className="text-xs font-mono bg-muted rounded p-3 overflow-x-auto"
        >
          {code.join("\n")}
        </pre>,
      );
      continue;
    }

    if (line.startsWith("#")) {
      flushParagraph();
      const level = line.match(/^#+/)![0].length;
      const text = line.replace(/^#+\s*/, "");
      blocks.push(
        <h4
          key={`h${key++}`}
          className={
            level <= 2
              ? "text-base font-semibold mt-4"
              : "text-sm font-semibold mt-3"
          }
        >
          {inline(text, `h${key}`)}
        </h4>,
      );
      i++;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
      flushParagraph();
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items: string[] = [];
      while (
        i < lines.length &&
        (/^\s*[-*]\s+/.test(lines[i]) ||
          /^\s*\d+\.\s+/.test(lines[i]) ||
          (items.length > 0 && /^\s{2,}\S/.test(lines[i])))
      ) {
        if (/^\s{2,}\S/.test(lines[i]) && items.length > 0) {
          items[items.length - 1] += ` ${lines[i].trim()}`; // wrapped line
        } else {
          items.push(lines[i].replace(/^\s*(?:[-*]|\d+\.)\s+/, ""));
        }
        i++;
      }
      const Tag = ordered ? "ol" : "ul";
      blocks.push(
        <Tag
          key={`l${key++}`}
          className={`text-sm space-y-1 pl-5 ${ordered ? "list-decimal" : "list-disc"}`}
        >
          {items.map((it, n) => (
            <li key={n}>{inline(it, `l${key}-${n}`)}</li>
          ))}
        </Tag>,
      );
      continue;
    }

    if (line.startsWith("|")) {
      flushParagraph();
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        if (!/^\|[\s|:-]+\|?$/.test(lines[i])) rows.push(tableRow(lines[i]));
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <div key={`t${key++}`} className="overflow-x-auto">
          <table className="text-sm border-collapse w-full">
            <thead>
              <tr className="text-left text-xs text-muted-foreground border-b">
                {head?.map((c, n) => (
                  <th key={n} className="py-1 pr-3 font-medium">
                    {inline(c, `th${key}-${n}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r} className="border-b align-top">
                  {row.map((c, n) => (
                    <td key={n} className="py-1.5 pr-3">
                      {inline(c, `td${key}-${r}-${n}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (line.startsWith(">")) {
      flushParagraph();
      const quote: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) {
        quote.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote
          key={`q${key++}`}
          className="border-l-2 border-amber-500/60 pl-3 text-sm text-muted-foreground"
        >
          {inline(quote.join(" "), `q${key}`)}
        </blockquote>,
      );
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      flushParagraph();
      blocks.push(<hr key={`r${key++}`} className="border-border" />);
      i++;
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      i++;
      continue;
    }

    paragraph.push(line.trim());
    i++;
  }
  flushParagraph();
  return blocks;
}

export function SetupGuide() {
  const [open, setOpen] = useState(false);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`${BASE}/setup-guide`);
      const body = await res.json();
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      if (!body.available)
        throw new Error("файл инструкции не найден в контейнере");
      setMarkdown(body.markdown as string);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, []);

  useEffect(() => {
    if (open && markdown === null && error === null) void load();
  }, [open, markdown, error, load]);

  return (
    <section className="space-y-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-sm font-semibold hover:underline"
        aria-expanded={open}
      >
        {open ? "▾" : "▸"} Инструкция: как включить вентиляторы материнской
        платы
      </button>
      {!open && (
        <p className="text-xs text-muted-foreground">
          Диагностика хоста, установка драйвера, что выключить в BIOS и как всё
          откатить.
        </p>
      )}
      {open && (
        <div className="rounded border p-4 space-y-3 max-h-[32rem] overflow-y-auto">
          {error && (
            <div className="text-sm text-red-600">
              Не удалось загрузить инструкцию: {error}
              <div className="text-xs text-muted-foreground mt-1">
                Текст лежит в репозитории: docs/cooling-motherboard-fans.md
              </div>
            </div>
          )}
          {!error && markdown === null && (
            <div className="text-sm text-muted-foreground">Загрузка…</div>
          )}
          {markdown !== null && render(markdown)}
        </div>
      )}
    </section>
  );
}
